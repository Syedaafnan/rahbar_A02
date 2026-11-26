"""A provider with no model and no network.

Use it for every unit test and for CI (section 6): your tests must never call a
real model. It is deterministic: the same input always gives the same output.
"""

from __future__ import annotations

from typing import Any

from ...schemas import Answer, Citation, Passage
from .prompt import build_messages


class StubProvider:
    name = "stub"

    def prompt(self, question: str, passages: list[Passage]) -> list[dict[str, str]]:
        return build_messages(question, passages, include_schema=False)

    def params(self) -> dict[str, Any]:
        return {"provider": "stub", "model": "stub", "temperature": 0.0}

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        if not passages:
            return Answer(
                reasoning="stub: nothing was retrieved",
                answer="(stub) No passages were retrieved for this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )
        top = passages[0]
        return Answer(
            reasoning="stub: cites the first retrieved passage",
            answer=f"(stub) A model would answer here from {len(passages)} passages. "
            f"The first one says: {top.text}",
            citations=[Citation(source=top.source, section=top.section, page=top.page)],
            answered_from_context=True,
            confidence="low",
        )
