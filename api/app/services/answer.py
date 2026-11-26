"""Turns a question into an AskResponse: the query pipeline (section 3.2).

  1. receive    the route validated 3-500 characters (AskRequest)
  2. cache      semantic cache lookup, skipped for follow-up questions
  3. embed      the question, with the same model as the indexer
  4. retrieve   top_k from Chroma; superseded documents only if explicitly asked for
  5. guardrail  drop retrieved passages that carry instructions for the model (A03 D4)
  6. floor      nothing above RELEVANCE_FLOOR -> abstain without calling the model
  7. generate   augmented prompt -> schema-constrained JSON at low temperature
  8. validate   Pydantic, retry once, then a safe fallback; drop invalid citations
  9. store      record the turn (Redis list rahbar:turns), and cache the answer
                only if it is safe to cache

A03 (RAGOps): every step is timed, and every request writes ONE trace record
(services/trace.py) with what was retrieved, filtered, dropped, prompted and answered.

Each step lives in its own module under services/ so it can be tested alone.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.prompt import trim_words
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, ContextPassage, Passage, RetrievedRef, RetrieveResponse
from . import guardrail
from .cache import Cache, NoCache
from .generation import generate_with_retry
from .grounding import above_floor, abstain_answer, is_follow_up, valid_citations, wants_old_version
from .trace import Trace, prompt_hash

log = logging.getLogger("rahbar.answer")


def answer_question(question: str, retriever: Retriever, provider: LLMProvider,
                    settings: Settings, cache: Cache | None = None,
                    no_cache: bool = False, debug: bool = False) -> tuple[AskResponse, str]:
    """Return (response, cache status). Cache status is "hit", "miss" or "skip"."""
    cache = cache or NoCache()
    follow_up = is_follow_up(question)
    include_old = wants_old_version(question)
    trace = Trace(request_id_var.get(), "ask", question, settings)

    # 3. embed once; the vector is shared by the cache and the retriever
    with trace.stage("embed"):
        vector = retriever.embed_query(question)
    _describe_index(trace, retriever, vector is not None)
    index_version = trace.record["collection"].get("index_version", "none")

    # 2. cache check (needs the vector, so it runs right after embedding)
    use_cache = vector is not None and getattr(cache, "enabled", False) and not follow_up \
        and not no_cache
    if use_cache and vector is not None:
        with trace.stage("cache"):
            hit = cache.lookup(vector, index_version)
        metrics.record_cache(hit is not None)
        if hit is not None:
            cached_from = hit.request_id
            hit.request_id = request_id_var.get()
            hit.trace_id = hit.request_id
            trace.set("cache", {"status": "hit", "threshold": settings.cache_threshold,
                                "cached_from_trace": cached_from})
            trace.set("retrieval", {"top_k": settings.top_k, "from_cache": True,
                                    "results": [r.model_dump() for r in hit.retrieved]})
            trace.set("response", _response_record(hit, "cache"))
            _finish(trace, hit, "cache")
            _log(settings, hit.retrieved, hit, cached=True, reason="cache")
            _record_turn(cache, question, hit, reason="cache", cached=True)
            return hit, "hit"
        trace.set("cache", {"status": "miss", "threshold": settings.cache_threshold})
    elif no_cache:
        trace.set("cache", {"status": "skip", "why": "no_cache requested (evaluation)"})
    elif follow_up:
        trace.set("cache", {"status": "skip", "why": "follow-up question"})

    # 4. retrieve
    with trace.stage("retrieve"):
        passages = retriever.retrieve(question, k=settings.top_k,
                                      include_superseded=include_old, vector=vector)
    metrics.RETRIEVAL_SECONDS.observe((trace.latency.get("embed", 0) +
                                       trace.latency.get("retrieve", 0)) / 1000)
    if passages:
        metrics.TOP1_SIMILARITY.observe(passages[0].score)

    # 5. guardrail, 6. floor
    with trace.stage("guardrail"):
        rail = guardrail.check(question, passages)
    for action in rail.actions:
        metrics.INJECTIONS.labels(target=action["target"], action=action["action"]).inc()
    trace.set("guardrail", rail.actions)
    evidence = above_floor(rail.passages, settings.relevance_floor)
    trace.set("retrieval", _retrieval_record(settings, retriever, passages, evidence,
                                             include_old))

    # 7-8. abstain, or generate + validate
    fallback = False
    prompt_passages: list[Passage] = []
    if not evidence:
        why = "every passage above the floor was dropped by the injection guardrail" \
            if rail.flagged and above_floor(passages, settings.relevance_floor) \
            else "nothing cleared the relevance floor"
        answer, reason = abstain_answer(why), "floor"
    else:
        # The prompt budget (ConfigMap): the best few passages, each trimmed. On a CPU
        # every prompt token costs ~45 ms. Retrieval and recall@5 still use all top_k,
        # and citations are checked against every passage above the floor.
        prompt_passages = [p.model_copy(update={"text": trim_words(p.text, settings.llm_passage_words)})
                           for p in evidence[:settings.llm_context_passages]]
        messages = provider.prompt(question, prompt_passages)
        trace.set("prompt", {"sha256": prompt_hash(messages), "messages": messages,
                             "passages": [p.chunk_id for p in prompt_passages]})
        with trace.stage("generate"):
            answer, fallback = generate_with_retry(provider, question, prompt_passages)
        with trace.stage("validate"):
            answer, reason = _ground(answer, evidence, fallback)
    trace.set("generation", {**provider.params(), "called": bool(evidence),
                             "fallback": fallback})
    if not answer.answered_from_context:
        metrics.ABSTENTIONS.labels(reason=reason).inc()

    response = _response(answer, passages, provider.name)
    response.reason = reason
    if debug:
        response.context = [ContextPassage(chunk_id=p.chunk_id, source=p.source,
                                           section=p.section, page=p.page, text=p.text)
                            for p in prompt_passages]
    trace.set("response", _response_record(response, reason))
    _finish(trace, response, reason)
    if debug:
        response.timings_ms = dict(trace.latency)
    _log(settings, response.retrieved, response, cached=False, reason=reason,
         include_superseded=include_old, follow_up=follow_up)

    # 9. store the turn (always), and a cache entry only if it is safe to cache:
    #    never follow-ups, abstentions, fallbacks, ungrounded or guarded answers
    _record_turn(cache, question, response, reason=reason, cached=False)
    skip_cache = follow_up or fallback or not answer.answered_from_context \
        or reason != "answered" or rail.flagged > 0
    if use_cache and not skip_cache and vector is not None:
        cache.store(question, vector, index_version, response)
    return response, ("miss" if use_cache else "skip")


def retrieve_only(question: str, retriever: Retriever, settings: Settings,
                  k: int | None = None) -> RetrieveResponse:
    """POST /api/retrieve: embed + retrieve, no guardrail, no LLM. The component-level
    tests (A03 B) score retrieval with it in seconds instead of minutes."""
    trace = Trace(request_id_var.get(), "retrieve", question, settings)
    include_old = wants_old_version(question)
    with trace.stage("embed"):
        vector = retriever.embed_query(question)
    _describe_index(trace, retriever, vector is not None)
    with trace.stage("retrieve"):
        passages = retriever.retrieve(question, k=k or settings.top_k,
                                      include_superseded=include_old, vector=vector)
    if passages:
        metrics.TOP1_SIMILARITY.observe(passages[0].score)
    trace.set("retrieval", _retrieval_record(settings, retriever, passages,
                                             above_floor(passages, settings.relevance_floor),
                                             include_old, k=k or settings.top_k))
    record = trace.finish()
    return RetrieveResponse(
        trace_id=record["trace_id"], collection=settings.collection,
        embed_model=settings.embed_model,
        retrieved=[RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section,
                                page=p.page, score=p.score) for p in passages],
        timings_ms=dict(trace.latency))


# ------------------------------------------------------------------ trace helpers

def _describe_index(trace: Trace, retriever: Retriever, has_vector: bool) -> None:
    try:
        meta = retriever.index_info()["metadata"] if has_vector else {}
    except Exception:
        meta = {}
    trace.record["collection"].update({
        "index_version": str(meta.get("index_version", "none")),
        "built_with_model": meta.get("embed_model"),
        "chunk_size": meta.get("chunk_size"), "chunk_overlap": meta.get("chunk_overlap")})
    try:
        trace.record["embedding"]["version"] = retriever.embedding_version()
    except Exception:
        trace.record["embedding"]["version"] = "unknown"


def _retrieval_record(settings: Settings, retriever: Retriever, passages: list[Passage],
                      evidence: list[Passage], include_old: bool,
                      k: int | None = None) -> dict[str, object]:
    return {
        "top_k": k or settings.top_k,
        "filters": retriever.where(include_old),
        "include_superseded": include_old,
        "relevance_floor": settings.relevance_floor,
        "results": [{"rank": n, "chunk_id": p.chunk_id, "doc_id": p.doc_id,
                     "source": p.source, "section": p.section, "page": p.page,
                     "doc_version": p.doc_version, "superseded": p.superseded,
                     "score": p.score} for n, p in enumerate(passages, start=1)],
        "reranker": "none",
        "reranked_order": [p.chunk_id for p in passages],
        "above_floor": [p.chunk_id for p in evidence],
        "top1_similarity": passages[0].score if passages else None,
    }


def _response_record(response: AskResponse, reason: str) -> dict[str, object]:
    return {"answer": response.answer, "answered_from_context": response.answered_from_context,
            "confidence": response.confidence, "reason": reason,
            "citations": [c.model_dump() for c in response.citations]}


def _finish(trace: Trace, response: AskResponse, reason: str) -> None:
    record = trace.finish()
    response.trace_id = record["trace_id"]
    for stage, ms in trace.latency.items():
        if stage != "total":
            metrics.STAGE_SECONDS.labels(stage=stage).observe(ms / 1000)
    metrics.STAGE_SECONDS.labels(stage="total").observe(trace.latency["total"] / 1000)
    metrics.record_answer(not response.answered_from_context, reason)


def _record_turn(cache: Cache, question: str, response: AskResponse, reason: str,
                 cached: bool) -> None:
    import time

    cache.record_turn({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": response.request_id,
        "question": question,
        "answer": response.answer,
        "citations": [c.model_dump() for c in response.citations],
        "answered_from_context": response.answered_from_context,
        "reason": reason,
        "cached": cached,
    })


def _ground(answer: Answer, evidence: list[Passage], fallback: bool) -> tuple[Answer, str]:
    """Apply the citation rule and name the outcome."""
    if fallback:
        return answer, "fallback"
    if not answer.answered_from_context:
        return abstain_answer("the model found no answer in the evidence"), "model"
    citations = valid_citations(answer.citations, evidence)
    if not citations:
        # It claims an answer but cites nothing we retrieved: that is not grounded.
        return abstain_answer("the answer cited no retrieved passage"), "ungrounded"
    return answer.model_copy(update={"citations": citations}), "answered"


def _response(answer: Answer, passages: list[Passage], provider: str) -> AskResponse:
    # answer.reasoning (the model's chain of thought) is deliberately not returned.
    rid = request_id_var.get()
    return AskResponse(
        request_id=rid,
        trace_id=rid,
        answer=answer.answer,
        citations=answer.citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=[RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section,
                                page=p.page, score=p.score) for p in passages],
        provider=provider,
    )


def _log(settings: Settings, retrieved: list[RetrievedRef], response: AskResponse,
         **fields: object) -> None:
    """One line per request with what was retrieved: where most failures are visible."""
    log.info("answered", extra={
        "top_k": settings.top_k,
        "chunk_ids": [p.chunk_id for p in retrieved],
        "scores": [p.score for p in retrieved],
        "answered_from_context": response.answered_from_context,
        "abstained": not response.answered_from_context,
        "citations": len(response.citations),
        **fields,
    })
