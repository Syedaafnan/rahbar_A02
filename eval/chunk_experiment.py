#!/usr/bin/env python3
"""Measure recall@k for several chunking settings (section 3.1: "tune chunking with
measurements, not guesses"). Uses the real code: the indexer's chunk/embed/store,
the api's ChromaRetriever and question rules, and run_eval.py's recall_at_k.

Step 1, once (needs Tesseract, so it runs in the indexer image):
    docker run --rm -v "$PWD/corpus/raw:/corpus:ro" -v "$PWD/eval/experiments:/out" \
      rahbar-indexer:dev --dump-docs /out/docs.json

Step 2, on the host (a scratch Chroma on :8001 and Ollama on :11434):
    docker run -d --rm --name chroma-exp -p 8001:8000 chromadb/chroma:1.0.12
    python eval/chunk_experiment.py --docs eval/experiments/docs.json

Each setting goes into its own scratch collection, so nothing touches the cluster.
Writes eval/experiments/chunking.json and prints the markdown table for PIPELINE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "indexer"), str(ROOT / "api"), str(ROOT / "eval")]

import yaml  # noqa: E402
from app.config import Settings  # noqa: E402
from app.retrieval.chroma import ChromaRetriever  # noqa: E402
from app.services.grounding import wants_old_version  # noqa: E402
from pipeline.config import get_settings  # noqa: E402
from pipeline.embed import embed  # noqa: E402
from pipeline.models import Document, Page  # noqa: E402
from pipeline.run import build_chunks  # noqa: E402
from pipeline.store import get_client, store  # noqa: E402
from run_eval import recall_at_k  # noqa: E402

# (strategy, size in words, overlap in words)
DEFAULT_CONFIGS = ["structure:200:20", "structure:400:50", "structure:800:100", "fixed:100:0"]


def load_docs(path: Path) -> list[Document]:
    docs = []
    for d in json.loads(path.read_text(encoding="utf-8")):
        pages = [Page(**p) for p in d.pop("pages")]
        docs.append(Document(**d, pages=pages))
    return docs


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def run_config(spec: str, docs: list[Document], questions: list[dict], args) -> dict:  # type: ignore[no-untyped-def]
    strategy, size, overlap = spec.split(":")
    name = f"{args.prefix}-{strategy}-{size}-{overlap}"
    idx = replace(get_settings(), chroma_host=args.chroma_host, chroma_port=args.chroma_port,
                  collection=name, ollama_url=args.ollama, chunk_size=int(size),
                  chunk_overlap=int(overlap), chunk_strategy=strategy)
    started = time.perf_counter()
    chunks = build_chunks(idx, docs)
    original = sum(len(p.text.split()) for d in docs for p in d.pages)
    stored = sum(len(c.text.split()) for c in chunks)
    if not args.keep:
        try:
            get_client(idx).delete_collection(name)        # start from scratch
        except Exception:
            pass
        store(chunks, embed([c.text for c in chunks], idx), idx)
    index_seconds = time.perf_counter() - started

    retriever = ChromaRetriever(Settings(retriever="chroma", chroma_host=args.chroma_host,
                                         chroma_port=args.chroma_port, collection=name,
                                         ollama_url=args.ollama, embed_model=idx.embed_model))
    rows, by_type = [], {}
    r1s, r3s, r5s = [], [], []
    for q in questions:
        old = args.no_superseded_filter or wants_old_version(q["question"])
        passages = retriever.retrieve(q["question"], k=5, include_superseded=old)
        retrieved = [{"source": p.source, "section": p.section, "score": p.score} for p in passages]
        r1 = recall_at_k(q.get("relevant") or [], retrieved, 1)
        r3 = recall_at_k(q.get("relevant") or [], retrieved, 3)
        r5 = recall_at_k(q.get("relevant") or [], retrieved, 5)
        if r5 is not None and r3 is not None and r1 is not None:
            r1s.append(r1)
            r3s.append(r3)
            r5s.append(r5)
            by_type.setdefault(q["type"], []).append(r5)
        rows.append({"id": q["id"], "type": q["type"], "must_abstain": bool(q.get("must_abstain")),
                     "recall@5": r5, "top1_score": passages[0].score if passages else None,
                     "top1": f"{passages[0].source} :: {passages[0].section}" if passages else None})
    return {"config": spec, "strategy": strategy, "size": int(size), "overlap": int(overlap),
            "chunks": len(chunks), "amplification": round(stored / max(1, original), 3),
            "index_seconds": round(index_seconds, 1), "recall@1": mean(r1s),
            "recall@3": mean(r3s), "recall@5": mean(r5s),
            "superseded_filter": not args.no_superseded_filter,
            "recall@5_by_type": {t: mean(v) for t, v in sorted(by_type.items())},
            "questions": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--docs", type=Path, required=True)
    parser.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden.yaml")
    parser.add_argument("--configs", nargs="+", default=DEFAULT_CONFIGS)
    parser.add_argument("--chroma-host", default="localhost")
    parser.add_argument("--chroma-port", type=int, default=8001)
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "experiments" / "chunking.json")
    parser.add_argument("--no-superseded-filter", action="store_true",
                        help="failure analysis stage E: retrieve superseded documents too")
    parser.add_argument("--prefix", default="exp", help="collection name prefix")
    parser.add_argument("--keep", action="store_true",
                        help="reuse an existing experiment collection instead of re-indexing")
    args = parser.parse_args()

    docs = load_docs(args.docs)
    questions = yaml.safe_load(args.golden.read_text(encoding="utf-8"))["questions"]
    results = []
    for spec in args.configs:
        print(f"--- {spec}", flush=True)
        results.append(run_config(spec, docs, questions, args))
        r = results[-1]
        print(f"    chunks {r['chunks']}  recall@3 {r['recall@3']}  recall@5 {r['recall@5']}  "
              f"by type {r['recall@5_by_type']}  ({r['index_seconds']} s)", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n| Strategy | Size (words) | Overlap (words) | Chunks | Amplification "
          "| recall@1 | recall@3 | recall@5 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        print(f"| {r['strategy']} | {r['size']} | {r['overlap']} | {r['chunks']} | "
              f"{r['amplification']} | {r['recall@1']} | {r['recall@3']} | {r['recall@5']} |")
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
