"""The LLM provider interface (section 3.5). Swapping providers is one file."""

from __future__ import annotations

from typing import Any, Protocol

from ...schemas import Answer, Passage


class LLMProvider(Protocol):
    name: str

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Answer the question from the passages, as an `Answer`."""
        ...

    def prompt(self, question: str, passages: list[Passage]) -> list[dict[str, str]]:
        """The exact messages generate() sends (A03: hashed and stored in the trace)."""
        ...

    def params(self) -> dict[str, Any]:
        """Model name and decoding parameters (A03 trace)."""
        ...
