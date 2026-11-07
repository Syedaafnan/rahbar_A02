"""Prometheus metrics at /metrics (section 4.8).

Request count and latency per route, plus the RAG-specific signals: retrieval and
generation latency, cache hits, abstentions, fallbacks, and tokens in and out.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

router = APIRouter()

_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram("rahbar_request_seconds", "HTTP request latency", ["path"], buckets=_SECONDS)
RETRIEVAL_SECONDS = Histogram("rahbar_retrieval_seconds", "Embed + vector search time",
                              buckets=_SECONDS)
GENERATION_SECONDS = Histogram("rahbar_generation_seconds", "LLM call time (all attempts)",
                               buckets=_SECONDS)
CACHE_LOOKUPS = Counter("rahbar_cache_lookups_total", "Semantic cache lookups", ["result"])
CACHE_HIT_RATIO = Gauge("rahbar_cache_hit_ratio", "Cache hits / cache lookups since start")
ABSTENTIONS = Counter("rahbar_abstentions_total", "Answers declined", ["reason"])
FALLBACKS = Counter("rahbar_fallbacks_total", "Canned answers after failed validation")
LLM_RETRIES = Counter("rahbar_llm_retries_total", "Second attempts after a bad LLM reply")
TOKENS = Histogram("rahbar_llm_tokens", "Tokens per LLM call", ["direction"],
                   buckets=(50, 100, 250, 500, 1000, 2000, 4000, 8000))

_hits = 0
_lookups = 0


def record_cache(hit: bool) -> None:
    global _hits, _lookups
    _lookups += 1
    _hits += int(hit)
    CACHE_LOOKUPS.labels(result="hit" if hit else "miss").inc()
    CACHE_HIT_RATIO.set(_hits / _lookups)


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
