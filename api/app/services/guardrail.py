"""Retrieval guardrail against prompt injection (A03 D4; section 3.4 of A02).

A02's defence was structural: retrieved text sits inside <evidence> tags and the
system prompt says it is data. That works with a capable model, but it is invisible:
nothing records that an attack was attempted. This rail makes the defence explicit
and observable:

  - a retrieved PASSAGE that contains an instruction aimed at the model is DROPPED
    before the prompt is built (it can still be seen in the trace, with its chunk id,
    document id and the pattern that matched);
  - a QUESTION that contains one is FLAGGED and still answered: the prompt treats the
    question as data, and dropping the user's whole question would help nobody.

Every action goes into the trace record (`guardrail` field) and into the
`rahbar_injection_flags_total` counter. Patterns are deliberately narrow: they must
not fire on ordinary rule text such as "students must attend 100% of classes"
(tests/test_ragops.py runs them over that kind of text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..schemas import Passage

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore-instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|"
        r"all|any|your|the|these)\b[^.\n]{0,25}\b(instructions?|rules?|prompts?|directions?|"
        r"guidelines|documents?|context)\b", re.IGNORECASE)),
    ("role-change", re.compile(
        r"\byou are (now|in)\b[^.\n]{0,30}\b(mode|assistant|ai|bot|dan|developer|unrestricted)\b"
        r"|\bact as (an? )?(unrestricted|different|new)\b", re.IGNORECASE)),
    ("system-override", re.compile(
        r"\b(system|admin|developer)\s+(override|prompt|message|mode|instruction)\b"
        r"|^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE)),
    ("forced-answer", re.compile(
        r"\b(always|instead)\s+(say|answer|respond|reply|tell (the )?(user|student)s?)\b"
        r"|\b(respond|reply|answer)\s+only\s+with\b"
        r"|\btell (the )?(user|student)s? that\b", re.IGNORECASE)),
    ("prompt-exfiltration", re.compile(
        r"\b(reveal|print|show|repeat|output)\b[^.\n]{0,30}\b(system prompt|your instructions|"
        r"hidden (rules|instructions))\b", re.IGNORECASE)),
]


@dataclass
class GuardrailResult:
    passages: list[Passage]                       # what may go into the prompt
    actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def flagged(self) -> int:
        return len(self.actions)


def find_injection(text: str) -> tuple[str, str] | None:
    """(pattern name, matched text) for the first pattern that matches, else None."""
    for name, pattern in PATTERNS:
        m = pattern.search(text)
        if m:
            return name, m.group(0)[:120]
    return None


def check(question: str, passages: list[Passage]) -> GuardrailResult:
    actions: list[dict[str, Any]] = []
    hit = find_injection(question)
    if hit:
        actions.append({"rail": "injection", "target": "question", "action": "flagged",
                        "pattern": hit[0], "matched": hit[1]})
    kept: list[Passage] = []
    for p in passages:
        hit = find_injection(p.text)
        if hit:
            actions.append({"rail": "injection", "target": "passage", "action": "dropped",
                            "chunk_id": p.chunk_id, "doc_id": p.doc_id, "source": p.source,
                            "doc_version": p.doc_version, "pattern": hit[0],
                            "matched": hit[1]})
        else:
            kept.append(p)
    return GuardrailResult(passages=kept, actions=actions)
