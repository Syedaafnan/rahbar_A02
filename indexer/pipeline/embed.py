"""Stage 4: Embed. One vector per chunk, from the pinned model only.

The model name comes from EMBED_MODEL in the ConfigMap, the same key the api reads.
nomic-embed-text expects a task prefix: "search_document: " for passages here, and
"search_query: " for questions in the api. Both sides must agree, like the model.
"""

from __future__ import annotations

import time

import httpx

from .config import IndexerSettings

DOCUMENT_PREFIX = "search_document: "
BATCH_SIZE = 16


def embed(texts: list[str], settings: IndexerSettings,
          transport: httpx.BaseTransport | None = None) -> list[list[float]]:
    """Embed with settings.embed_model, in batches, retrying a failed batch once.
    `transport` lets the tests replace Ollama with a fake; leave it None otherwise."""
    vectors: list[list[float]] = []
    with httpx.Client(base_url=settings.ollama_url, timeout=300.0, transport=transport) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [DOCUMENT_PREFIX + t for t in texts[start:start + BATCH_SIZE]]
            vectors.extend(_embed_batch(client, settings.embed_model, batch))
    if vectors and len({len(v) for v in vectors}) != 1:
        raise RuntimeError("the embedding model returned vectors of different sizes")
    return vectors


def _embed_batch(client: httpx.Client, model: str, batch: list[str]) -> list[list[float]]:
    for attempt in (1, 2):
        try:
            response = client.post("/api/embed", json={"model": model, "input": batch})
            response.raise_for_status()
            embeddings: list[list[float]] = response.json()["embeddings"]
            if len(embeddings) != len(batch):
                raise RuntimeError(f"asked for {len(batch)} vectors, got {len(embeddings)}")
            return embeddings
        except (httpx.HTTPError, KeyError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(3)
    raise AssertionError("unreachable")
