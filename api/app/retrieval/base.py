"""The retriever interface. The rest of the API only ever sees this."""

from __future__ import annotations

from typing import Any, Protocol

from ..schemas import Passage


class Retriever(Protocol):
    name: str

    def embed_query(self, question: str) -> list[float] | None:
        """The question's vector, or None when this retriever has no embeddings (stub).
        Computed once per request and shared with the semantic cache."""
        ...

    def retrieve(self, question: str, k: int, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        """Return up to k passages, best first. Superseded documents are left out
        unless include_superseded is True."""
        ...

    def index_info(self) -> dict[str, Any]:
        """{"count": int, "metadata": dict} of the collection. Raises if unreachable."""
        ...
