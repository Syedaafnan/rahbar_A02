"""The real retriever (sections 3.2 and 4.4). It only READS Chroma.

1. Embed the question with EMBED_MODEL (same model and prefix rules as the indexer).
2. Ask Chroma for the top k, with a metadata filter `superseded == False` unless the
   user explicitly asked about an older version. This filter is what stops the
   system quoting a repealed 2023 clause (failure mode E); a prompt cannot do that.
3. Turn Chroma's cosine distance into similarity: score = 1 - distance.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from ..embedding import embed_query
from ..schemas import Passage

INDEX_INFO_TTL = 15.0   # seconds; readiness and the cache key do not need fresher data


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.Client(timeout=30.0)
        self._client: Any = None
        self._info: tuple[float, dict[str, Any]] | None = None

    def _chroma(self) -> Any:
        """Connect on first use, not at import: if Chroma is down when the pod starts,
        the pod should start anyway and simply report not-ready."""
        if self._client is None:
            import chromadb  # the thin client, from chromadb-client in requirements.txt
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host, port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False))
        return self._client

    def _collection(self) -> Any:
        return self._chroma().get_collection(self.settings.collection)

    def embed_query(self, question: str) -> list[float] | None:
        return embed_query(question, self.settings, self.http)

    def retrieve(self, question: str, k: int, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        vector = vector or embed_query(question, self.settings, self.http)
        where = None if include_superseded else {"superseded": False}
        result = self._collection().query(query_embeddings=[vector], n_results=k, where=where,
                                          include=["documents", "metadatas", "distances"])
        passages = []
        for chunk_id, text, meta, distance in zip(result["ids"][0], result["documents"][0],
                                                  result["metadatas"][0], result["distances"][0]):
            passages.append(Passage(
                chunk_id=chunk_id, text=text or "", source=str(meta.get("source", "")),
                section=str(meta.get("section", "")), page=meta.get("page"),
                score=round(1.0 - float(distance), 4),
                doc_version=str(meta.get("doc_version", "")),
                superseded=bool(meta.get("superseded", False)),
            ))
        return passages

    def index_info(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._info and now - self._info[0] < INDEX_INFO_TTL:
            return self._info[1]
        collection = self._collection()
        info = {"count": int(collection.count()), "metadata": dict(collection.metadata or {})}
        self._info = (now, info)
        return info
