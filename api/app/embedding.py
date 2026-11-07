"""Question embeddings, with the SAME model the indexer used (section 3.2).

The indexer embeds passages as "search_document: <text>"; nomic-embed-text wants
questions as "search_query: <text>". Mixing the prefixes up quietly lowers recall.
"""

from __future__ import annotations

import httpx

from .config import Settings

QUERY_PREFIX = "search_query: "


def embed_query(question: str, settings: Settings, client: httpx.Client) -> list[float]:
    response = client.post(f"{settings.ollama_url}/api/embed",
                           json={"model": settings.embed_model, "input": [QUERY_PREFIX + question]},
                           timeout=30.0)
    response.raise_for_status()
    vector: list[float] = response.json()["embeddings"][0]
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return dot / norm if norm else 0.0
