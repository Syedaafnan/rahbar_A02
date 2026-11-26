#!/usr/bin/env python3
"""Query coverage (A03 E2/E4, paper 4.2.7): do our tests still look like real usage?

For every live query, find the most similar golden question (cosine of the embedding
model under test, query prefix). A live query is covered when that similarity is at
least `coverage.similarity_threshold` (eval/gate.yaml or the rahbar-gate ConfigMap).
Report the covered share, list the uncovered queries, and group them into topics.
Exit 1 when the share is below `coverage.min_share` (the paper flags a gap under 85%).

    python eval/coverage_check.py --label before
    python eval/coverage_check.py --label after --golden eval/golden.jsonl eval/golden_e4.jsonl

Topics: uncovered queries are grouped automatically (greedy clustering: a query joins
the first group whose seed it matches at >= --group-threshold), and each group also
shows the topic we wrote next to the query in live_queries.jsonl, for checking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ragops_metrics import cosine  # noqa: E402


def load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    return rows


def embed(texts: list[str]) -> list[list[float]]:
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    prefix = os.environ.get("EMBED_QUERY_PREFIX", "search_query: ")
    model = os.environ.get("EMBED_MODEL", "nomic-embed-text")
    r = httpx.post(f"{url}/api/embed", json={"model": model, "input": [prefix + t for t in texts]},
                   timeout=300.0)
    r.raise_for_status()
    return r.json()["embeddings"]


def nearest(vector: list[float], golden: dict[str, list[float]]) -> tuple[str, float]:
    best = max(golden.items(), key=lambda kv: cosine(vector, kv[1]))
    return best[0], cosine(vector, best[1])


def group_topics(items: list[tuple[str, list[float], str]], threshold: float) -> list[dict[str, Any]]:
    """Greedy clustering of (id, vector, written topic) into groups."""
    groups: list[dict[str, Any]] = []
    for qid, vec, topic in items:
        for g in groups:
            if cosine(vec, g["seed"]) >= threshold:
                g["queries"].append(qid)
                g["written_topics"].add(topic)
                break
        else:
            groups.append({"seed": vec, "queries": [qid], "written_topics": {topic}})
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description="Coverage of live queries by the golden set.")
    ap.add_argument("--golden", type=Path, nargs="+", default=[HERE / "golden.jsonl"])
    ap.add_argument("--live", type=Path, default=HERE / "live_queries.jsonl")
    ap.add_argument("--config", type=Path, default=Path(os.environ.get("GATE_CONFIG", HERE / "gate.yaml")))
    ap.add_argument("--label", default="run")
    ap.add_argument("--group-threshold", type=float, default=0.60)
    ap.add_argument("--out-dir", type=Path, default=Path(os.environ.get("RESULTS_DIR", HERE / "results")))
    args = ap.parse_args()

    cfg = (yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}).get("coverage", {})
    threshold = float(cfg.get("similarity_threshold", 0.75))
    min_share = float(cfg.get("min_share", 0.85))
    golden = load_jsonl(args.golden)
    live = load_jsonl([args.live])
    gvec = dict(zip([g["id"] for g in golden], embed([g["question"] for g in golden])))
    gtext = {g["id"]: g["question"] for g in golden}
    lvec = embed([q["query"] for q in live])

    rows = []
    for q, vec in zip(live, lvec):
        gid, score = nearest(vec, gvec)
        rows.append({"id": q["id"], "query": q["query"], "topic": q.get("topic", ""),
                     "in_domain": q.get("in_domain"), "nearest_golden": gid,
                     "nearest_question": gtext[gid], "similarity": round(score, 4),
                     "covered": score >= threshold})
    covered = sum(r["covered"] for r in rows)
    share = covered / len(rows)
    uncovered = [r for r in rows if not r["covered"]]
    by_id = {r["id"]: v for r, v in zip(rows, lvec)}
    groups = group_topics([(r["id"], by_id[r["id"]], r["topic"]) for r in uncovered],
                          args.group_threshold)
    topics = [{"queries": [next(r["query"] for r in uncovered if r["id"] == i) for i in g["queries"]],
               "ids": g["queries"], "written_topics": sorted(g["written_topics"])} for g in groups]

    print(f"COVERAGE  label={args.label}  golden={len(golden)} questions "
          f"({', '.join(p.name for p in args.golden)})  live={len(rows)} queries")
    print(f"threshold: similarity >= {threshold}; gap if covered share < {min_share:.0%}\n")
    print(f"{'id':<5} {'sim':>6}  {'covered':<8} {'query':<58} nearest golden")
    for r in sorted(rows, key=lambda r: r["similarity"]):
        print(f"{r['id']:<5} {r['similarity']:>6.3f}  {'yes' if r['covered'] else 'NO':<8} "
              f"{r['query'][:58]:<58} {r['nearest_golden']}: {r['nearest_question'][:50]}")
    in_dom = [r for r in rows if r["in_domain"]]
    out_dom = [r for r in rows if r["in_domain"] is False]
    print(f"\ncovered: {covered}/{len(rows)} = {share:.1%}  "
          f"(in-domain {sum(r['covered'] for r in in_dom)}/{len(in_dom)}, "
          f"out-of-domain {sum(r['covered'] for r in out_dom)}/{len(out_dom)})")
    print(f"\nUNCOVERED QUERIES, grouped into {len(topics)} topics:")
    for n, t in enumerate(topics, start=1):
        print(f"  topic {n} ({', '.join(t['written_topics'])}): " + " | ".join(t["queries"]))
    by_topic: dict[str, list[str]] = {}
    for r in uncovered:
        by_topic.setdefault(r["topic"] or "?", []).append(r["query"])
    print(f"\nUNCOVERED QUERIES by the topic written next to each live query "
          f"({len(by_topic)} topics):")
    for topic, qs in sorted(by_topic.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        in_dom = any(r["in_domain"] for r in uncovered if r["topic"] == topic)
        dom = "in-domain" if in_dom else "out of domain"
        print(f"  {topic:<17} ({len(qs)}, {dom}): " + " | ".join(qs))
    ok = share >= min_share
    print(f"\nRESULT: {'OK' if ok else 'COVERAGE GAP'}: {share:.1%} "
          f"{'>=' if ok else '<'} {min_share:.0%}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = args.out_dir / f"{stamp}-coverage-{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "timestamp": stamp,
                               "embed_model": os.environ.get("EMBED_MODEL", "nomic-embed-text"),
                               "golden": [p.name for p in args.golden], "golden_questions": len(golden),
                               "threshold": threshold, "min_share": min_share,
                               "covered": covered, "total": len(rows), "share": round(share, 4),
                               "ok": ok, "uncovered_topics": topics, "uncovered_by_written_topic": by_topic, "rows": rows}, indent=1),
                   encoding="utf-8")
    print(f"result file: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
