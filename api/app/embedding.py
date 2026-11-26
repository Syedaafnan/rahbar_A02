"""Question embeddings, with the SAME model the indexer used (section 3.2).

The indexer embeds passages as "search_document: <text>"; nomic-embed-text wants
questions as "search_query: <text>". Mixing the prefixes up quietly lowers recall.
"""

from __future__ import annotations

import httpx

from .config import Settings

_versions: dict[str, str] = {}


def embed_query(question: str, settings: Settings, client: httpx.Client) -> list[float]:
    response = client.post(f"{settings.ollama_url}/api/embed",
                           json={"model": settings.embed_model,
                                 "input": [settings.embed_query_prefix + question]},
                           timeout=30.0)
    response.raise_for_status()
    vector: list[float] = response.json()["embeddings"][0]
    return vector


def embedding_version(settings: Settings, client: httpx.Client) -> str:
    """'<model>@<digest>' from Ollama, so a trace names the exact model weights, not
    just a tag that can be re-pulled. Looked up once per model and cached."""
    model = settings.embed_model
    if model not in _versions:
        try:
            tags = client.get(f"{settings.ollama_url}/api/tags", timeout=5.0).json()
            for m in tags.get("models", []):
                if m.get("name") in (model, f"{model}:latest") or m.get("model") == model:
                    _versions[model] = f"{m.get('name')}@{str(m.get('digest', ''))[:12]}"
                    break
            else:
                _versions[model] = f"{model}@unknown"
        except Exception:
            return f"{model}@unknown"            # not cached: try again next time
    return _versions[model]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return dot / norm if norm else 0.0
