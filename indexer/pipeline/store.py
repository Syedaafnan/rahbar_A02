"""Stage 5: Store. The only code in the whole system that writes to Chroma.

What happens to the old index while the api is serving from it (ADR 0001):

- Same embedding model as the existing collection: update IN PLACE. Upsert every
  new chunk, delete the chunks that no longer exist, then stamp a new
  index_version. The api keeps answering from a complete index the whole time.
- Different model (or no collection yet): the old vectors are useless, so drop
  the collection and build a new one. The api's readiness probe reports
  "not ready" until the new collection is filled, which is the honest answer.

The collection metadata records the embedding model and vector dimension (the pin
the api checks, section 4.4) and index_version, which the api uses as its cache
key prefix, so a re-index makes every old cache entry unreachable (section 3.3).
"""

from __future__ import annotations

import time
from typing import Any

from .config import IndexerSettings
from .models import Chunk

WRITE_BATCH = 200


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings,
          client: Any = None) -> int:
    """Write every chunk with its vector and metadata. Return the collection's count."""
    if len(chunks) != len(vectors) or not chunks:
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    if len({c.id for c in chunks}) != len(chunks):
        raise ValueError("chunk ids are not unique")
    client = client or get_client(settings)
    metadata = {
        "embed_model": settings.embed_model,
        "dimension": len(vectors[0]),
        "index_version": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
    }

    collection = _existing(client, settings.collection)
    same_model = collection is not None and \
        collection.metadata.get("embed_model") == metadata["embed_model"] and \
        collection.metadata.get("dimension") == metadata["dimension"]
    if not same_model:
        if collection is not None:
            client.delete_collection(settings.collection)
        collection = client.create_collection(
            name=settings.collection, metadata={"hnsw:space": "cosine", **metadata})

    for start in range(0, len(chunks), WRITE_BATCH):
        part = chunks[start:start + WRITE_BATCH]
        collection.upsert(
            ids=[c.id for c in part],
            documents=[c.text for c in part],
            embeddings=vectors[start:start + WRITE_BATCH],
            metadatas=[c.metadata() for c in part],
        )

    if same_model:
        new_ids = {c.id for c in chunks}
        stale = [i for i in collection.get(include=[])["ids"] if i not in new_ids]
        if stale:
            collection.delete(ids=stale)
        # Last step on purpose: the api sees the new version only once all writes are done.
        collection.modify(metadata=metadata)
    return int(collection.count())


def _existing(client: Any, name: str) -> Any:
    try:
        return client.get_collection(name)
    except Exception:  # chromadb raises NotFoundError / ValueError depending on version
        return None
