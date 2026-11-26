"""One trace record per query (A03 D1, paper 3.2.3 and Table 2: traceability).

Each /api/ask and /api/retrieve call writes exactly one JSON line with everything
needed to explain the answer afterwards from the trace alone:

  trace_id, timestamp, release, pod
  query        user query, rewritten query (we do not rewrite: same text, rewriter "none")
  embedding    model, version (Ollama digest), query prefix
  collection   name, index_version, the model the collection was built with
  retrieval    top_k, filters applied, relevance floor, every retrieved chunk with
               doc id, document version, superseded flag and score; reranked order
               (no reranker: retrieval order), the chunks that cleared the floor
  guardrail    every action of the injection rail (services/guardrail.py)
  prompt       sha256 of the exact messages, and the messages themselves
  generation   provider, model and parameters (temperature, seed, num_ctx), fallback
  response     answer, citations, answered_from_context, confidence, reason
  cache        hit | miss | skip, and the trace that produced a cached answer
  latency_ms   per stage: embed, cache, retrieve, guardrail, generate, validate, total

Where it goes (A03 F4): standard output, as one line starting {"type": "trace", ...}
so `kubectl logs deploy/api | grep '"type": "trace"'` finds it; and, when TRACE_DIR
is set (the traces PVC), appended to TRACE_DIR/traces-<pod>.jsonl for the report
scripts (scripts/trace_show.py, scripts/trace_summary.py). One file per pod, so
replicas never interleave half-lines.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import Settings

_lock = threading.Lock()


def prompt_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


class Trace:
    def __init__(self, trace_id: str, kind: str, question: str, settings: Settings) -> None:
        self.settings = settings
        self._started = time.perf_counter()
        self.latency: dict[str, float] = {}
        self.record: dict[str, Any] = {
            "type": "trace",
            "kind": kind,                                    # ask | retrieve
            "trace_id": trace_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) +
            f".{int(time.time() * 1000) % 1000:03d}Z",
            "release": settings.release,
            "pod": os.environ.get("HOSTNAME", "local"),
            "query": {"user_query": question,
                      "rewritten_query": " ".join(question.split()),
                      "rewriter": "none (whitespace normalised only)"},
            "embedding": {"model": settings.embed_model, "version": "",
                          "query_prefix": settings.embed_query_prefix},
            "collection": {"name": settings.collection},
            "retrieval": None,
            "guardrail": [],
            "prompt": None,
            "generation": None,
            "response": None,
            "cache": {"status": "skip"},
            "latency_ms": self.latency,
        }

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.latency[name] = round(self.latency.get(name, 0.0) +
                                       (time.perf_counter() - start) * 1000, 2)

    def set(self, key: str, value: Any) -> None:
        self.record[key] = value

    def finish(self) -> dict[str, Any]:
        self.latency["total"] = round((time.perf_counter() - self._started) * 1000, 2)
        write(self.record, self.settings.trace_dir)
        return self.record


def write(record: dict[str, Any], trace_dir: str = "") -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if trace_dir:
            try:
                folder = Path(trace_dir)
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / f"traces-{record.get('pod', 'local')}.jsonl").open(
                        "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:   # a full or missing volume must not fail the answer
                sys.stdout.write(json.dumps({"type": "trace-write-error",
                                             "trace_id": record.get("trace_id"),
                                             "error": str(exc)}) + "\n")
