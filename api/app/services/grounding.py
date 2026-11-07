"""Grounding rules (section 3.4), as small pure functions.

- relevance floor: a passage below RELEVANCE_FLOOR similarity is not evidence;
  if nothing clears it, the api abstains without calling the model at all.
- citation check: every citation must match a passage retrieved for THIS request.
  Anything else is dropped; an answer left with no valid citation is not grounded.
- question type: is it a follow-up (never cached), and does it explicitly ask about
  an older version (the only time superseded documents may be retrieved)?
"""

from __future__ import annotations

import re

from ..schemas import Answer, Citation, Passage

ABSTAIN_TEXT = ("I could not find this in the documents I have. The RAHBAR corpus does "
                "not cover this question, so I will not guess. Please check with the "
                "relevant university office.")
FALLBACK_TEXT = ("Sorry, I could not produce a reliable answer just now. Please try "
                 "again, or rephrase the question.")

# "and what about ...", "what if it ...", "also, ..." : they lean on an earlier turn.
_FOLLOW_UP = re.compile(
    r"^(and|but|also|so|then|what about|how about|what if|same for|and if)\b"
    r"|^(is|does|can|was) (it|that|this|they)\b"
    r"|\b(the above|that one|this one|as you said|you said|previous answer)\b", re.I)
# "under the 2023 rules", "the old policy", "before the revision", "previous version"
_OLD_VERSION = re.compile(
    r"\b(19|20)\d\d\b(?!\s*-\s*\d)|\b(old|older|previous|earlier|former|superseded|"
    r"outdated|repealed|original)\s+(version|rules?|policy|handbook|edition|regulations?)\b"
    r"|\bbefore (the )?(revision|update|change)\b", re.I)
CURRENT_YEARS = {"2026", "2027"}


def is_follow_up(question: str) -> bool:
    return bool(_FOLLOW_UP.search(question.strip()))


def wants_old_version(question: str) -> bool:
    """True when the user explicitly asks about an older version of a rule."""
    for match in _OLD_VERSION.finditer(question):
        text = match.group(0)
        if text.isdigit() and text in CURRENT_YEARS:
            continue                  # "2026" is the current year, not an old version
        return True
    return False


def above_floor(passages: list[Passage], floor: float) -> list[Passage]:
    return [p for p in passages if p.score >= floor]


def valid_citations(citations: list[Citation], retrieved: list[Passage]) -> list[Citation]:
    """Keep only citations that name a retrieved passage.

    A citation is resolved to ONE retrieved passage of the same source, in this order:
      1. exact section;
      2. a shortened section ("Withdrawal from a Course" for
         "4. Registration > Withdrawal from a Course");
      3. a clause number ("6.6") that appears in that passage's text: small models
         cite the clause they read rather than the heading;
      4. the only retrieved passage from that source.
    The citation is then rewritten to that passage's exact source/section/page, so a
    returned citation always names a chunk retrieved for this request. A citation that
    resolves to nothing was never retrieved, so it is dropped.
    """
    kept: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for c in citations:
        match = _find(c, retrieved)
        if match is None:
            continue
        key = (match.source, match.section)
        if key not in seen:
            seen.add(key)
            kept.append(Citation(source=match.source, section=match.section, page=match.page))
    return kept


def _find(c: Citation, retrieved: list[Passage]) -> Passage | None:
    same_source = [p for p in retrieved if p.source.strip().lower() == c.source.strip().lower()]
    wanted = c.section.strip().lower()
    for p in same_source:
        if p.section.strip().lower() == wanted:
            return p
    for p in same_source:
        section = p.section.strip().lower()
        if wanted and (wanted in section or section.endswith(wanted)):
            return p
    clause = re.match(r"^\(?(\d+(?:\.\d+)+)\)?", wanted)        # "6.6", "4.25", "(1.21)"
    if clause:
        token = re.compile(rf"(?<![\d.]){re.escape(clause.group(1))}(?![\d])")
        holding = [p for p in same_source if token.search(p.text)]
        if len(holding) >= 1:
            return holding[0]
    if len(same_source) == 1:
        return same_source[0]
    return None


def abstain_answer(reason: str) -> Answer:
    return Answer(reasoning=f"abstained: {reason}", answer=ABSTAIN_TEXT, citations=[],
                  answered_from_context=False, confidence="low")


def fallback_answer() -> Answer:
    return Answer(reasoning="fallback after invalid model output", answer=FALLBACK_TEXT,
                  citations=[], answered_from_context=False, confidence="low")
