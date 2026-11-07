"""Semantic cache in Redis (section 3.3, ADR 0002).

A hit is a stored question whose embedding has cosine similarity >= CACHE_THRESHOLD
with the new question. Each entry is one Redis key:

    rahbar:cache:<index_version>:<sha1 of the question>  ->  JSON {question, vector, response}

Invalidate on re-index: the key starts with the collection's index_version, which
the indexer Job changes on every run. After a re-index, lookups only scan the new
prefix, so every old answer is unreachable at once, and the old keys expire by TTL.
(The indexer cannot reach Redis: the NetworkPolicy allows redis from the api only.)

Lookup is a plain scan of the current prefix plus a cosine in Python. With the
hundreds of entries a course project produces that takes milliseconds, and it
needs only plain Redis, no vector module.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol

from ..config import Settings
from ..embedding import cosine
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")
MAX_SCAN = 2000
TURNS_KEY = "rahbar:turns"     # the conversation log: newest first, capped
MAX_TURNS = 1000


class Cache(Protocol):
    def lookup(self, vector: list[float], index_version: str) -> AskResponse | None: ...
    def store(self, question: str, vector: list[float], index_version: str,
              response: AskResponse) -> None: ...
    def record_turn(self, turn: dict[str, Any]) -> None: ...
    def ping(self) -> bool: ...


class NoCache:
    """Used with the stub retriever and in unit tests: never hits, stores nothing."""

    enabled = False

    def lookup(self, vector: list[float], index_version: str) -> AskResponse | None:
        return None

    def store(self, question: str, vector: list[float], index_version: str,
              response: AskResponse) -> None:
        return None

    def record_turn(self, turn: dict[str, Any]) -> None:
        return None

    def ping(self) -> bool:
        return True


class RedisCache:
    enabled = True

    def __init__(self, settings: Settings, client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis(host=settings.redis_host, port=settings.redis_port,
                                 socket_timeout=2, socket_connect_timeout=2)
        self.redis = client
        self.threshold = settings.cache_threshold
        self.expire_seconds = settings.cache_ttl_seconds

    @staticmethod
    def _prefix(index_version: str) -> str:
        """Keys are scoped by index_version: a re-index bumps it, which invalidates
        every older entry at once (they then expire on their own)."""
        return f"rahbar:cache:{index_version}:"

    def lookup(self, vector: list[float], index_version: str) -> AskResponse | None:
        best_score, best = 0.0, None
        try:
            keys = []
            for key in self.redis.scan_iter(match=self._prefix(index_version) + "*", count=200):
                keys.append(key)
                if len(keys) >= MAX_SCAN:
                    break
            for raw in (self.redis.mget(keys) if keys else []):
                if not raw:
                    continue
                entry = json.loads(raw)
                score = cosine(vector, entry["vector"])
                if score > best_score:
                    best_score, best = score, entry
        except Exception as exc:   # a broken cache must never break an answer
            log.warning("cache lookup failed", extra={"error": str(exc)[:200]})
            return None
        if best is not None and best_score >= self.threshold:
            # No question text in logs (ADR 0004): the similarity is what we tune with.
            log.info("cache hit", extra={"similarity": round(best_score, 4)})
            return AskResponse.model_validate(best["response"])
        return None

    def store(self, question: str, vector: list[float], index_version: str,
              response: AskResponse) -> None:
        key = self._prefix(index_version) + hashlib.sha1(
            " ".join(question.lower().split()).encode()).hexdigest()
        value = json.dumps({"question": question, "vector": vector,
                            "response": response.model_dump()})
        try:
            self.redis.set(key, value, ex=self.expire_seconds)
        except Exception as exc:
            log.warning("cache store failed", extra={"error": str(exc)[:200]})

    def record_turn(self, turn: dict[str, Any]) -> None:
        """Step 8, store the turn: one JSON record per request in a capped list, so the
        last MAX_TURNS questions and outcomes can be reviewed (and wrong answers found)
        after the pod that served them is gone. Newest first; older turns fall off."""
        try:
            self.redis.lpush(TURNS_KEY, json.dumps(turn, ensure_ascii=False))
            self.redis.ltrim(TURNS_KEY, 0, MAX_TURNS - 1)
        except Exception as exc:   # losing a history record must never break an answer
            log.warning("turn store failed", extra={"error": str(exc)[:200]})

    def ping(self) -> bool:
        return bool(self.redis.ping())


def get_cache(settings: Settings) -> Cache:
    if settings.redis_host and settings.retriever != "stub":
        return RedisCache(settings)
    return NoCache()
