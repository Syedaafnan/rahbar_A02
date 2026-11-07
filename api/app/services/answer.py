"""Turns a question into an AskResponse: the query pipeline (section 3.2).

  1. receive    the route validated 3-500 characters (AskRequest)
  2. cache      semantic cache lookup, skipped for follow-up questions
  3. embed      the question, with the same model as the indexer
  4. retrieve   top_k from Chroma; superseded documents only if explicitly asked for
  5. floor      nothing above RELEVANCE_FLOOR -> abstain without calling the model
  6. generate   augmented prompt -> schema-constrained JSON at low temperature
  7. validate   Pydantic, retry once, then a safe fallback; drop invalid citations
  8. store      record the turn (Redis list rahbar:turns), and cache the answer
                only if it is safe to cache

Each step lives in its own module under services/ so it can be tested alone.
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.prompt import trim_words
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from .cache import Cache, NoCache
from .generation import generate_with_retry
from .grounding import (abstain_answer, above_floor, is_follow_up, valid_citations,
                        wants_old_version)

log = logging.getLogger("rahbar.answer")


def answer_question(question: str, retriever: Retriever, provider: LLMProvider,
                    settings: Settings, cache: Cache | None = None) -> tuple[AskResponse, str]:
    """Return (response, cache status). Cache status is "hit", "miss" or "skip"."""
    cache = cache or NoCache()
    follow_up = is_follow_up(question)
    include_old = wants_old_version(question)

    # 3. embed once; the vector is shared by the cache and the retriever
    started = time.perf_counter()
    vector = retriever.embed_query(question)
    index_version = str(retriever.index_info()["metadata"].get("index_version", "none")) \
        if vector is not None else "none"

    # 2. cache check (needs the vector, so it runs right after embedding)
    use_cache = vector is not None and getattr(cache, "enabled", False) and not follow_up
    if use_cache and vector is not None:
        hit = cache.lookup(vector, index_version)
        metrics.record_cache(hit is not None)
        if hit is not None:
            hit.request_id = request_id_var.get()
            _log(settings, hit.retrieved, hit, cached=True, reason="cache")
            _record_turn(cache, question, hit, reason="cache", cached=True)
            return hit, "hit"

    # 4. retrieve
    passages = retriever.retrieve(question, k=settings.top_k,
                                  include_superseded=include_old, vector=vector)
    metrics.RETRIEVAL_SECONDS.observe(time.perf_counter() - started)
    evidence = above_floor(passages, settings.relevance_floor)

    # 5-7. abstain, or generate + validate
    fallback = False
    if not evidence:
        answer, reason = abstain_answer("nothing cleared the relevance floor"), "floor"
    else:
        # The prompt budget (ConfigMap): the best few passages, each trimmed. On a CPU
        # every prompt token costs ~45 ms. Retrieval and recall@5 still use all top_k,
        # and citations are checked against every passage above the floor.
        prompt_passages = [p.model_copy(update={"text": trim_words(p.text, settings.llm_passage_words)})
                           for p in evidence[:settings.llm_context_passages]]
        answer, fallback = generate_with_retry(provider, question, prompt_passages)
        answer, reason = _ground(answer, evidence, fallback)
    if not answer.answered_from_context:
        metrics.ABSTENTIONS.labels(reason=reason).inc()

    response = _response(answer, passages, provider.name)
    _log(settings, response.retrieved, response, cached=False, reason=reason,
         include_superseded=include_old, follow_up=follow_up)

    # 8. store the turn (always), and a cache entry only if it is safe to cache:
    #    never follow-ups, abstentions, fallbacks or ungrounded answers
    _record_turn(cache, question, response, reason=reason, cached=False)
    skip_cache = follow_up or fallback or not answer.answered_from_context \
        or reason != "answered"
    if use_cache and not skip_cache and vector is not None:
        cache.store(question, vector, index_version, response)
    return response, ("miss" if use_cache else "skip")


def _record_turn(cache: Cache, question: str, response: AskResponse, reason: str,
                 cached: bool) -> None:
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
    return AskResponse(
        request_id=request_id_var.get(),
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
