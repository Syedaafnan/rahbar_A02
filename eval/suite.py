#!/usr/bin/env python3
"""The A03 evaluation suite (Part B, paper 4.2.5 and Table 1): ONE command runs all
three levels against a running RAHBAR and writes a timestamped result file.

    python eval/suite.py --api http://localhost:8000 --label baseline
    python eval/suite.py --api http://api-candidate:8000 --label candidate \
        --baseline eval/results/baseline.json --gate        # exit 1 if the gate fails

Levels
  module      the embedding model alone (Ollama): mean cosine of 15 paraphrase pairs
              vs 15 unrelated pairs and the gap; embedding time per chunk; determinism
              (embed the same texts twice, largest absolute difference)
  component   retrieval alone (POST /api/retrieve, no LLM): recall@3, recall@5, MRR,
              nDCG@5 on the golden set; then the noise test (typos in every query)
  end-to-end  answers (POST /api/ask, cache bypassed): faithfulness (LLM judge, or the
              word-overlap fallback), agreement of the judge with our hand labels,
              abstention correctness, citation validity and correctness, latency p50/p95
Stochasticity: seeds are fixed (typos, LLM seed, judge seed, temperature 0 for the
judge); --repeat 2 runs everything twice and reports |run1 - run2| per metric.

Configuration comes from the same environment the api reads (EMBED_MODEL,
EMBED_QUERY_PREFIX, EMBED_DOC_PREFIX, OLLAMA_URL, CHROMA_*), so the eval Job for the
candidate simply uses the candidate's ConfigMap. Judge: JUDGE=llm|overlap|none,
JUDGE_PROVIDER=ollama|hosted, JUDGE_MODEL (default LLM_MODEL).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ragops_metrics import (
    add_typos,
    citation_correct,
    citation_validity,
    cosine,
    diff_metrics,
    mean,
    ndcg_at_k,
    overlap_support,
    percentile,
    recall_at_k,
    reciprocal_rank,
)

HERE = Path(__file__).resolve().parent
SEED = int(os.environ.get("SEED", 42))


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def post(client: httpx.Client, url: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST with retries for the Ingress/Service hiccups right after a rollout, and for a
    500 while Chroma restarts (one 500 killed a 2-hour baseline run once)."""
    for attempt in range(6):
        try:
            r = client.post(url, json=body)
            if r.status_code not in (500, 502, 503, 504) or attempt == 5:
                r.raise_for_status()
                return r.json()
        except httpx.TransportError:
            if attempt == 5:
                raise
        time.sleep(5)
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ module level

class Embedder:
    def __init__(self) -> None:
        self.url = env("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self.model = env("EMBED_MODEL", "nomic-embed-text")
        self.qprefix = env("EMBED_QUERY_PREFIX", "search_query: ")
        self.dprefix = env("EMBED_DOC_PREFIX", "search_document: ")
        self.http = httpx.Client(timeout=120.0)

    def __call__(self, texts: list[str], prefix: str) -> list[list[float]]:
        r = self.http.post(f"{self.url}/api/embed",
                           json={"model": self.model, "input": [prefix + t for t in texts]})
        r.raise_for_status()
        return r.json()["embeddings"]


def sample_chunks(n: int = 24) -> tuple[list[str], str]:
    """Real chunk texts from the collection under test (read only, Chroma REST API),
    for the embedding-time measurement. Falls back to golden questions if unreachable."""
    host, port = env("CHROMA_HOST", "chroma"), env("CHROMA_PORT", "8000")
    name = env("CHROMA_COLLECTION", "rahbar")
    base = f"http://{host}:{port}/api/v2/tenants/default_tenant/databases/default_database"
    try:
        with httpx.Client(timeout=10.0) as http:
            col = http.get(f"{base}/collections/{name}").json()
            got = http.post(f"{base}/collections/{col['id']}/get",
                            json={"limit": n, "include": ["documents"]}).json()
            docs = [d for d in got.get("documents", []) if d]
            if docs:
                return docs, f"chroma:{name}"
    except Exception:
        pass
    return [], "unavailable"


def module_level(embedder: Embedder, pairs: list[dict[str, Any]],
                 fallback_texts: list[str]) -> dict[str, Any]:
    texts = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    vecs = dict(zip(texts, embedder(texts, embedder.qprefix)))
    para = [cosine(vecs[p["a"]], vecs[p["b"]]) for p in pairs if p["kind"] == "paraphrase"]
    unrel = [cosine(vecs[p["a"]], vecs[p["b"]]) for p in pairs if p["kind"] == "unrelated"]

    chunks, source = sample_chunks()
    if not chunks:
        chunks, source = fallback_texts[:24], "golden questions (chunks unavailable)"
    embedder(chunks[:2], embedder.dprefix)                       # warm the model
    started = time.perf_counter()
    for c in chunks:
        embedder([c], embedder.dprefix)
    one_by_one = (time.perf_counter() - started) * 1000 / len(chunks)
    started = time.perf_counter()
    embedder(chunks, embedder.dprefix)
    batched = (time.perf_counter() - started) * 1000 / len(chunks)

    probe = chunks[:8]
    first = embedder(probe, embedder.dprefix)
    second = embedder(probe, embedder.dprefix)
    max_diff = max(abs(x - y) for a, b in zip(first, second) for x, y in zip(a, b))
    return {
        "embed_model": embedder.model,
        "dimension": len(first[0]),
        "paraphrase_pairs": len(para), "unrelated_pairs": len(unrel),
        "paraphrase_mean_cosine": round(sum(para) / len(para), 4),
        "unrelated_mean_cosine": round(sum(unrel) / len(unrel), 4),
        "gap": round(sum(para) / len(para) - sum(unrel) / len(unrel), 4),
        "paraphrase_min": round(min(para), 4), "unrelated_max": round(max(unrel), 4),
        "separable": min(para) > max(unrel),
        "embed_ms_per_chunk": round(one_by_one, 2),
        "embed_ms_per_chunk_batched": round(batched, 2),
        "timing_sample": f"{len(chunks)} chunks from {source}",
        "determinism_max_abs_diff": float(max_diff),
        "determinism_texts": len(probe),
    }


# ------------------------------------------------------------------ component level

def component_level(client: httpx.Client, api: str, golden: list[dict[str, Any]],
                    typos: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = []
    for q in golden:
        if not q.get("relevant"):
            continue
        question = add_typos(q["question"], seed=SEED + hash_int(q["id"])) if typos \
            else q["question"]
        res = post(client, f"{api}/api/retrieve", {"question": question, "k": 5})
        got = res["retrieved"]
        rows.append({
            "id": q["id"], "category": q["category"], "query": question,
            "trace_id": res["trace_id"],
            "recall@3": recall_at_k(q["relevant"], got, 3),
            "recall@5": recall_at_k(q["relevant"], got, 5),
            "rr": reciprocal_rank(q["relevant"], got),
            "ndcg@5": ndcg_at_k(q["relevant"], got, 5),
            "top1": got[0]["score"] if got else None,
            "retrieved": [f'{p["source"]} :: {p["section"]} (p{p.get("page")}, {p["score"]})'
                          for p in got],
            "retrieve_ms": res["timings_ms"].get("retrieve", 0) + res["timings_ms"].get("embed", 0),
        })
    summary: dict[str, Any] = {
        "questions": len(rows),
        "recall@3": mean(r["recall@3"] for r in rows),
        "recall@5": mean(r["recall@5"] for r in rows),
        "mrr": mean(r["rr"] for r in rows),
        "ndcg@5": mean(r["ndcg@5"] for r in rows),
        "retrieve_ms_p50": percentile([r["retrieve_ms"] for r in rows], 50),
    }
    by_cat: dict[str, dict[str, Any]] = {}
    for cat in sorted({r["category"] for r in rows}):
        sub = [r for r in rows if r["category"] == cat]
        by_cat[cat] = {"n": len(sub), "recall@5": mean(r["recall@5"] for r in sub),
                       "mrr": mean(r["rr"] for r in sub)}
    summary["by_category"] = by_cat
    return summary, rows


def hash_int(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


# ------------------------------------------------------------------ end-to-end level

JUDGE_PROMPT = """You are checking a student-help chatbot. Decide whether its ANSWER to the QUESTION is supported by the CONTEXT passages it was given.

QUESTION: {question}

CONTEXT:
{context}

ANSWER: {answer}

Rules:
- A short answer (a number, "yes", "no") is supported if the context states that fact for this question.
- Supported means the context states it or directly implies it. Extra wording is fine; new facts are not.
- Copy ONE sentence from the CONTEXT, word for word, that best supports the answer (empty if none).

Reply with JSON only: {{"evidence": "<sentence copied from the context>", "supported": true or false}}"""

# Why this prompt (our first version gave faithfulness 0.08 on the pre-Batch-B run):
# llama3.2:3b, shown only the answer, called "80%" unsupported although the context
# says it word for word, and once copied our prompt's own placeholder into its reply.
# The question is now included (a bare "80%" or "No" cannot be checked without it), and
# the judge must quote its evidence: a verdict of "supported" counts only if that quote
# really is in the context, so a small model cannot approve an answer by inventing one.


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class Judge:
    def __init__(self) -> None:
        self.mode = env("JUDGE", "llm")
        self.provider = env("JUDGE_PROVIDER", "ollama")
        self.model = env("JUDGE_MODEL", env("LLM_MODEL", "llama3.2:3b"))
        self.http = httpx.Client(timeout=float(env("JUDGE_TIMEOUT_SECONDS", "300")))

    def describe(self) -> dict[str, Any]:
        if self.mode != "llm":
            return {"mode": self.mode}
        return {"mode": "llm", "provider": self.provider, "model": self.model,
                "temperature": 0, "seed": SEED, "prompt": "evidence-quoted v2"}

    def __call__(self, answer: str, context: list[dict[str, Any]],
                 question: str = "") -> dict[str, Any]:
        ctx = "\n\n".join(f"[{c['source']} / {c['section']}]\n{c['text']}" for c in context)
        overlap = overlap_support(answer, ctx)
        if self.mode == "overlap":
            return {"supported": overlap >= 0.6, "overlap": round(overlap, 3), "by": "overlap"}
        if self.mode == "none":
            return {"supported": None, "overlap": round(overlap, 3), "by": "none"}
        prompt = JUDGE_PROMPT.format(question=question, context=ctx, answer=answer)
        try:
            raw = self._ask(prompt)
            verdict = json.loads(raw)
            said = verdict.get("supported") is True or str(verdict.get("supported")).lower() == "true"
            evidence = str(verdict.get("evidence") or "")
            quoted = bool(evidence) and (_norm(evidence) in _norm(ctx)
                                         or overlap_support(evidence, ctx) >= 0.9)
            return {"supported": said and quoted, "judge_said": said,
                    "evidence": evidence[:300], "evidence_in_context": quoted,
                    "overlap": round(overlap, 3), "by": f"llm:{self.model}"}
        except Exception as exc:   # judge failure: fall back, and say so in the result
            return {"supported": overlap >= 0.6, "overlap": round(overlap, 3),
                    "by": f"overlap (judge error: {type(exc).__name__})"}

    def _ask(self, prompt: str) -> str:
        if self.provider == "hosted":
            r = self.http.post(f"{env('HOSTED_BASE_URL', '').rstrip('/')}/chat/completions",
                               headers={"Authorization": f"Bearer {env('LLM_API_KEY', '')}"},
                               json={"model": self.model, "temperature": 0, "seed": SEED,
                                     "response_format": {"type": "json_object"},
                                     "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return str(r.json()["choices"][0]["message"]["content"])
        r = self.http.post(f"{env('OLLAMA_URL', 'http://localhost:11434').rstrip('/')}/api/chat",
                           json={"model": self.model, "stream": False, "format": "json",
                                 "keep_alive": "30m",
                                 "options": {"temperature": 0, "seed": SEED, "num_ctx": 4096},
                                 "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return str(r.json()["message"]["content"])


def answer_sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def e2e_level(client: httpx.Client, api: str, golden: list[dict[str, Any]], judge: Judge,
              labels: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = []
    for q in golden:
        started = time.perf_counter()
        res = post(client, f"{api}/api/ask", {"question": q["question"], "no_cache": True,
                                              "debug": True})
        wall = (time.perf_counter() - started) * 1000
        answered = bool(res["answered_from_context"])
        valid, total = citation_validity(res)
        verdict = judge(res["answer"], res.get("context") or [], q["question"]) \
            if answered else None
        expect = q.get("expect")
        rows.append({
            "id": q["id"], "category": q["category"], "type": q.get("type"),
            "must_abstain": q["must_abstain"], "trace_id": res.get("trace_id"),
            "answer": res["answer"], "answer_sha": answer_sha(res["answer"]),
            "answered_from_context": answered, "reason": res.get("reason"),
            "citations": res.get("citations", []),
            "citations_valid": valid, "citations_total": total,
            "citation_correct": citation_correct(res, q.get("relevant") or []),
            "expect_found": (str(expect).lower() in res["answer"].lower()) if expect and answered
            else (None if q["must_abstain"] else False),
            "faithful": verdict["supported"] if verdict else None,
            "judge": verdict, "latency_ms": round(wall, 1),
            "question": q["question"], "context": res.get("context") or [],
            "stage_ms": res.get("timings_ms", {}),
        })
        print(f"    e2e {q['id']:<4} {wall / 1000:6.1f}s  answered={answered!s:<5} "
              f"{res.get('reason', '')}", flush=True)
    must = [r for r in rows if r["must_abstain"]]
    answerable = [r for r in rows if not r["must_abstain"]]
    judged = [r for r in rows if r["faithful"] is not None]
    cited = sum(r["citations_total"] for r in rows)
    valid = sum(r["citations_valid"] for r in rows)
    lat = [r["latency_ms"] for r in rows]
    stage_names = sorted({s for r in rows for s in r["stage_ms"]})
    agreement = judge_agreement(rows, labels)
    summary = {
        "questions": len(rows),
        "faithfulness": round(sum(bool(r["faithful"]) for r in judged) / len(judged), 4)
        if judged else None,
        "faithfulness_judged": len(judged),
        "judge": judge.describe(),
        "judge_agreement": agreement,
        "abstention_correctness": round(sum(not r["answered_from_context"] for r in must) /
                                        len(must), 4) if must else None,
        "must_abstain_questions": len(must),
        "false_abstention_rate": round(sum(not r["answered_from_context"] for r in answerable) /
                                       len(answerable), 4) if answerable else None,
        "citation_validity": round(valid / cited, 4) if cited else 1.0,
        "citations_checked": cited,
        "citation_correctness": mean(float(r["citation_correct"]) for r in rows
                                     if r["citation_correct"] is not None),
        "expect_found_rate": mean(float(r["expect_found"]) for r in answerable
                                  if r["expect_found"] is not None),
        "latency_ms": {"p50": percentile(lat, 50), "p95": percentile(lat, 95),
                       "max": max(lat) if lat else None},
        "stage_latency_ms_p95": {s: percentile([r["stage_ms"][s] for r in rows
                                                if s in r["stage_ms"]], 95) for s in stage_names},
    }
    return summary, rows


def judge_agreement(rows: list[dict[str, Any]],
                    labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """How often the judge agrees with our hand labels (eval/judge_labels.jsonl). A
    label counts only when it was made for exactly this answer text (same hash)."""
    pairs = []
    for r in rows:
        lab = labels.get(r["id"])
        if lab and lab.get("answer_sha") == r["answer_sha"] and r["faithful"] is not None:
            pairs.append((bool(lab["human_faithful"]), bool(r["faithful"])))
    if not pairs:
        return {"labelled": 0, "agreement": None,
                "note": "no hand label matches this run's answers: run eval/label_answers.py"}
    agree = sum(h == j for h, j in pairs)
    return {"labelled": len(pairs), "agreement": round(agree / len(pairs), 4),
            "judge_says_faithful_human_not": sum((not h) and j for h, j in pairs),
            "judge_says_unfaithful_human_yes": sum(h and (not j) for h, j in pairs)}


# ------------------------------------------------------------------ one full run

def run_once(api: str, golden: list[dict[str, Any]], pairs: list[dict[str, Any]],
             skip_e2e: bool, labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    embedder, judge = Embedder(), Judge()
    with httpx.Client(timeout=float(env("EVAL_TIMEOUT_SECONDS", "400"))) as client:
        print("  module level ...", flush=True)
        module = module_level(embedder, pairs, [q["question"] for q in golden])
        print("  component level (clean, then typos) ...", flush=True)
        clean, clean_rows = component_level(client, api, golden, typos=False)
        noisy, noisy_rows = component_level(client, api, golden, typos=True)
        delta = {k: round(noisy[k] - clean[k], 4) for k in ("recall@3", "recall@5", "mrr", "ndcg@5")
                 if clean.get(k) is not None and noisy.get(k) is not None}
        e2e, e2e_rows = ({}, []) if skip_e2e else e2e_level(client, api, golden, judge, labels)
    return {"module": module,
            "component": {"clean": clean, "typos": noisy, "typos_minus_clean": delta},
            "e2e": e2e,
            "per_question": {"component": clean_rows, "component_typos": noisy_rows,
                             "e2e": e2e_rows}}


def summary_markdown(res: dict[str, Any]) -> str:
    m, c, e = res["module"], res["component"], res["e2e"]
    lines = [f"# Evaluation summary: {res['label']}  ({res['timestamp']})", "",
             f"api `{res['api']}` · collection `{res['config'].get('CHROMA_COLLECTION')}` · "
             f"embedding `{m['embed_model']}` · golden {res['golden_counts']}", "",
             "| Level | Metric | Value |", "| --- | --- | ---: |"]

    def row(level: str, name: str, value: Any) -> None:
        lines.append(f"| {level} | {name} | {value if value is not None else '-'} |")
    row("module", "paraphrase mean cosine", m["paraphrase_mean_cosine"])
    row("module", "unrelated mean cosine", m["unrelated_mean_cosine"])
    row("module", "gap", m["gap"])
    row("module", "embedding ms / chunk (one at a time)", m["embed_ms_per_chunk"])
    row("module", "embedding ms / chunk (batched)", m["embed_ms_per_chunk_batched"])
    row("module", "determinism: max abs diff", m["determinism_max_abs_diff"])
    for k in ("recall@3", "recall@5", "mrr", "ndcg@5"):
        row("component", k, c["clean"][k])
    for k, v in c["typos_minus_clean"].items():
        row("component", f"{k} with typos (delta)", f"{c['typos'][k]} ({v:+})")
    if e:
        row("end-to-end", f"faithfulness ({e['judge'].get('mode')})", e["faithfulness"])
        ja = e["judge_agreement"]
        row("end-to-end", "judge vs hand labels", f"{ja['agreement']} on {ja['labelled']}"
            if ja["labelled"] else "not labelled")
        row("end-to-end", "abstention correctness", e["abstention_correctness"])
        row("end-to-end", "false abstention rate (answerable)", e["false_abstention_rate"])
        row("end-to-end", "citation validity", e["citation_validity"])
        row("end-to-end", "citation correctness", e["citation_correctness"])
        row("end-to-end", "expected fact found", e["expect_found_rate"])
        row("end-to-end", "latency p50 (ms)", e["latency_ms"]["p50"])
        row("end-to-end", "latency p95 (ms)", e["latency_ms"]["p95"])
    if res.get("stochasticity"):
        lines += ["", "## Stochasticity (|run 1 - run 2|)", "", "| Metric | Difference |",
                  "| --- | ---: |"]
        headline = ("module.paraphrase_mean_cosine", "module.unrelated_mean_cosine", "module.gap",
                    "module.determinism_max_abs_diff", "component.clean.recall@3",
                    "component.clean.recall@5", "component.clean.mrr", "component.clean.ndcg@5",
                    "component.typos.recall@5", "e2e.faithfulness", "e2e.abstention_correctness",
                    "e2e.citation_validity", "e2e.citation_correctness", "e2e.expect_found_rate")
        diffs = res["stochasticity"]["metric_diff"]
        for k in headline:
            if k in diffs:
                lines.append(f"| {k} | {diffs[k]} |")
        lat = res["stochasticity"]["latency_diff_ms"]
        for k in ("e2e.latency_ms.p50", "e2e.latency_ms.p95"):
            if k in lat:
                lines.append(f"| {k} (ms) | {lat[k]} |")
        lines.append(f"\nAnswers that changed between runs: "
                     f"{res['stochasticity']['answers_changed']}")
    if res.get("gate"):
        lines += ["", "## Release gate", "", "```", res["gate"]["table"], "```"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the A03 evaluation suite (all three levels).")
    ap.add_argument("--api", default=env("API_URL", "http://localhost:8000"))
    ap.add_argument("--label", default=env("EVAL_LABEL", "run"))
    ap.add_argument("--golden", type=Path, nargs="+",
                    default=[Path(p) for p in env("GOLDEN_FILES", str(HERE / "golden.jsonl")).split(",")])
    ap.add_argument("--pairs", type=Path, default=HERE / "embedding_pairs.jsonl")
    ap.add_argument("--labels", type=Path, default=HERE / "judge_labels.jsonl")
    ap.add_argument("--out-dir", type=Path, default=Path(env("RESULTS_DIR", str(HERE / "results"))))
    ap.add_argument("--repeat", type=int, default=int(env("EVAL_REPEAT", "1")))
    ap.add_argument("--skip-e2e", action="store_true")
    ap.add_argument("--gate", action="store_true", help="apply the release gate, exit 1 on fail")
    ap.add_argument("--gate-config", type=Path, default=Path(env("GATE_CONFIG", str(HERE / "gate.yaml"))))
    ap.add_argument("--baseline", type=Path, default=None,
                    help="baseline result file (default: <out-dir>/baseline.json)")
    ap.add_argument("--save-baseline", action="store_true",
                    help="also copy this result to <out-dir>/baseline.json")
    args = ap.parse_args()

    api = args.api.rstrip("/")
    golden = load_jsonl(args.golden)
    pairs = load_jsonl([args.pairs])
    labels = {r["id"]: r for r in load_jsonl([args.labels])} if args.labels.exists() else {}
    counts: dict[str, int] = {}
    for q in golden:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print(f"RAHBAR evaluation suite  label={args.label}  api={api}  golden={counts}  "
          f"repeat={args.repeat}", flush=True)

    runs = []
    for n in range(args.repeat):
        print(f"\nrun {n + 1} of {args.repeat}", flush=True)
        runs.append(run_once(api, golden, pairs, args.skip_e2e, labels))
    result: dict[str, Any] = {
        "label": args.label, "timestamp": stamp, "api": api,
        "config": {k: os.environ.get(k) for k in ("EMBED_MODEL", "EMBED_QUERY_PREFIX",
                                                  "CHROMA_COLLECTION", "LLM_MODEL", "LLM_PROVIDER",
                                                  "TOP_K", "RELEVANCE_FLOOR", "TEMPERATURE",
                                                  "LLM_SEED", "CHUNK_SIZE", "CHUNK_OVERLAP")},
        "seed": SEED, "golden": [str(p.name) for p in args.golden], "golden_counts": counts,
        **{k: runs[0][k] for k in ("module", "component", "e2e", "per_question")},
    }
    if len(runs) > 1:
        a, b = runs[0], runs[1]
        metric_diff = diff_metrics({k: a[k] for k in ("module", "component", "e2e")},
                                   {k: b[k] for k in ("module", "component", "e2e")})
        changed = [x["id"] for x, y in zip(a["per_question"]["e2e"], b["per_question"]["e2e"])
                   if x["answer_sha"] != y["answer_sha"]]
        result["stochasticity"] = {
            "runs": len(runs),
            "metric_diff": {k: v for k, v in metric_diff.items() if "latency" not in k
                            and "_ms" not in k},
            "latency_diff_ms": {k: v for k, v in metric_diff.items() if "latency" in k or "_ms" in k},
            "answers_changed": changed,
            "run2": {k: b[k] for k in ("module", "component", "e2e")},
        }

    exit_code = 0
    if args.gate:
        sys.path.insert(0, str(HERE))
        from gate import decide, evaluate, load_thresholds, table
        baseline_path = args.baseline or args.out_dir / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) \
            if baseline_path.exists() else None
        rows = evaluate(result, baseline, load_thresholds(args.gate_config))
        released = decide(rows)
        result["gate"] = {"baseline": str(baseline_path) if baseline else None,
                          "config": str(args.gate_config), "rules": rows, "released": released,
                          "table": table(rows, released, args.label)}
        exit_code = 0 if released else 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{stamp}-{args.label}.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    md = summary_markdown(result)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    shutil.copyfile(out, args.out_dir / f"latest-{args.label}.json")
    if args.save_baseline:
        shutil.copyfile(out, args.out_dir / "baseline.json")
    print("\n" + md)
    print(f"result file: {out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
