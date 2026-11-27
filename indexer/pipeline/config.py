"""Indexer configuration, from the same ConfigMap the API reads (section 4.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexerSettings:
    embed_model: str        # EMBED_MODEL: must equal the API's value (section 4.4)
    chroma_host: str
    chroma_port: int
    collection: str
    corpus_dir: str         # where the documents and _index.json sit inside the Job's pod
    chunk_size: int
    chunk_overlap: int
    ollama_url: str
    chunk_strategy: str = "structure"   # CHUNK_STRATEGY: structure | fixed (baseline only)
    # --- A03 (RAGOps) -------------------------------------------------------------
    # Task prefixes differ per embedding model (nomic wants "search_document: ";
    # mxbai-embed-large wants none for passages). They travel with EMBED_MODEL in the
    # ConfigMap, so a candidate model brings its own.
    embed_doc_prefix: str = "search_document: "
    data_dir: str = ""                  # DATA_DIR: the data lake PVC; "" = no lake (tests)
    near_dup_threshold: float = 0.96    # NEAR_DUP_THRESHOLD: whole-document cosine (calibrated)
    # LAKE_READ_ONLY=true: verify and index, but never move files in the lake. The
    # candidate indexer Jobs share the live data lake and must not rearrange it.
    lake_read_only: bool = False


def get_settings() -> IndexerSettings:
    return IndexerSettings(
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=int(os.environ.get("CHROMA_PORT", 8000)),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        corpus_dir=os.environ.get("CORPUS_DIR", "/corpus"),
        chunk_size=int(os.environ.get("CHUNK_SIZE", 400)),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", 50)),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        chunk_strategy=os.environ.get("CHUNK_STRATEGY", "structure"),
        embed_doc_prefix=os.environ.get("EMBED_DOC_PREFIX", "search_document: "),
        data_dir=os.environ.get("DATA_DIR", ""),
        near_dup_threshold=float(os.environ.get("NEAR_DUP_THRESHOLD", 0.96)),
        lake_read_only=os.environ.get("LAKE_READ_ONLY", "false").lower() == "true",
    )
