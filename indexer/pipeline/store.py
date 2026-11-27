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
from collections.abc import Callable
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


# --------------------------------------------------------------------------------
# A03 (RAGOps): incremental updates (paper 4.2.4, "update retrieval sources").
#
# Chunk ids are "<doc_id>:<hash of the chunk text>" (pipeline/chunk.py), so:
#   - an unchanged chunk has an id that is already in the collection: no embedding;
#   - a changed document produces new ids for its changed chunks, and its old ids
#     are no longer wanted: only that document's changed chunks are touched;
#   - a deleted document's ids are all unwanted: every one of them is deleted, so no
#     orphan chunk is left (prove it with `python -m pipeline.run --count-by-doc`);
#   - an archived (superseded) version keeps its ids and vectors; only its metadata
#     changes (superseded=true, status=archived), so restoring it is instant.

def sync(chunks: list[Chunk], embed_fn: Callable[[list[str]], list[list[float]]],
         settings: IndexerSettings, client: Any = None,
         keep_ids: set[str] | None = None) -> dict[str, Any]:
    """Make the collection hold exactly `chunks` (plus `keep_ids`, the last good chunks
    of a document whose new version was quarantined), embedding only what is new.
    Return the counts the ingestion report shows."""
    keep_ids = keep_ids or set()
    if len({c.id for c in chunks}) != len(chunks):
        raise ValueError("chunk ids are not unique")
    client = client or get_client(settings)
    collection = _existing(client, settings.collection)
    rebuilt = False
    if collection is not None and collection.metadata.get("embed_model") != settings.embed_model:
        # Vectors from another model are useless: start a fresh collection.
        client.delete_collection(settings.collection)
        collection, rebuilt = None, True
    if collection is None:
        collection = client.create_collection(
            name=settings.collection,
            metadata={"hnsw:space": "cosine", "embed_model": settings.embed_model,
                      "index_version": "empty"})
        rebuilt = True

    existing = _all_metadata(collection)
    wanted = {c.id: c for c in chunks}
    new = [c for c in chunks if c.id not in existing]
    stale = [i for i in existing if i not in wanted and i not in keep_ids]
    relabel = [c for c in chunks if c.id in existing and existing[c.id] != c.metadata()]

    for start in range(0, len(relabel), WRITE_BATCH):
        part = relabel[start:start + WRITE_BATCH]
        collection.update(ids=[c.id for c in part], metadatas=[c.metadata() for c in part])

    dimension = collection.metadata.get("dimension")
    for start in range(0, len(new), WRITE_BATCH):
        part = new[start:start + WRITE_BATCH]
        vectors = embed_fn([c.text for c in part])
        if len(vectors) != len(part):
            raise ValueError(f"{len(part)} chunks but {len(vectors)} vectors")
        dimension = len(vectors[0])
        collection.upsert(ids=[c.id for c in part], documents=[c.text for c in part],
                          embeddings=vectors, metadatas=[c.metadata() for c in part])

    for start in range(0, len(stale), WRITE_BATCH):
        collection.delete(ids=stale[start:start + WRITE_BATCH])

    changed = bool(new or stale or relabel)
    metadata = {k: v for k, v in collection.metadata.items() if not k.startswith("hnsw:")}
    metadata.update({"embed_model": settings.embed_model, "chunk_size": settings.chunk_size,
                     "chunk_overlap": settings.chunk_overlap})
    if dimension:
        metadata["dimension"] = int(dimension)
    if changed or metadata.get("index_version") in (None, "empty"):
        # A new index_version also invalidates the api's semantic cache (section 3.3).
        # Nothing changed -> same version -> cached answers stay valid.
        metadata["index_version"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    collection.modify(metadata=metadata)

    stale_by_doc: dict[str, int] = {}
    for i in stale:
        doc = str(existing[i].get("doc_id", i.split(":")[0]))
        stale_by_doc[doc] = stale_by_doc.get(doc, 0) + 1
    new_by_doc: dict[str, int] = {}
    for c in new:
        new_by_doc[c.doc_id] = new_by_doc.get(c.doc_id, 0) + 1
    return {
        "collection": settings.collection, "embed_model": settings.embed_model,
        "rebuilt": rebuilt, "chunks_wanted": len(chunks), "embedded": len(new),
        "skipped_unchanged": len(chunks) - len(new), "metadata_updated": len(relabel),
        "deleted": len(stale), "kept": len(keep_ids & set(existing)), "embedded_by_doc": new_by_doc, "deleted_by_doc": stale_by_doc,
        "index_version": metadata["index_version"], "total": int(collection.count()),
    }


def count_by_doc(settings: IndexerSettings, client: Any = None) -> dict[str, dict[str, Any]]:
    """doc_id -> {chunks, status, doc_version}: the orphan check for A2."""
    client = client or get_client(settings)
    collection = _existing(client, settings.collection)
    out: dict[str, dict[str, Any]] = {}
    if collection is None:
        return out
    for meta in _all_metadata(collection).values():
        doc = str(meta.get("doc_id", "?"))
        row = out.setdefault(doc, {"chunks": 0, "status": set(), "doc_version": set()})
        row["chunks"] += 1
        row["status"].add(str(meta.get("status", "active")))
        row["doc_version"].add(str(meta.get("doc_version", "")))
    return {k: {"chunks": v["chunks"], "status": ",".join(sorted(v["status"])),
                "doc_version": ",".join(sorted(v["doc_version"]))} for k, v in sorted(out.items())}


def set_status(settings: IndexerSettings, doc_ids: dict[str, bool], client: Any = None) -> int:
    """Flip superseded/status on every chunk of the given documents (restore/rollback).
    doc_ids maps doc_id -> superseded. Returns the number of chunks relabelled."""
    client = client or get_client(settings)
    collection = client.get_collection(settings.collection)
    changed = 0
    for doc_id, superseded in doc_ids.items():
        got = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
        ids, metas = got["ids"], got["metadatas"]
        if not ids:
            continue
        for m in metas:
            m["superseded"] = superseded
            m["status"] = "archived" if superseded else "active"
        collection.update(ids=ids, metadatas=metas)
        changed += len(ids)
    if changed:
        metadata = {k: v for k, v in collection.metadata.items() if not k.startswith("hnsw:")}
        metadata["index_version"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        collection.modify(metadata=metadata)
    return changed


def _all_metadata(collection: Any) -> dict[str, dict[str, Any]]:
    got = collection.get(include=["metadatas"])
    return {i: dict(m or {}) for i, m in zip(got["ids"], got["metadatas"])}
