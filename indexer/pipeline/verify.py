"""Verification stage (A03 A1, paper 4.2.2): runs after extraction and cleaning, before
chunking. Every document gets pass or fail, with a reason, for four checks:

  quality       the text is really there: not empty, not garbled, extraction worked
  completeness  title, source, date or version and page count are present, and the
                text is not truncated (pages missing, PDF cut off before %%EOF)
  recency       no newer version of the same document (same `family`) is in the lake
  uniqueness    not an exact duplicate (same SHA-256) and not a near-duplicate (whole-
                document embedding similarity at or above NEAR_DUP_THRESHOLD) of a
                document that was kept earlier in this run. The document vector is the
                mean of 6 windows spread across the text: embedding only the first
                1,500 words made the two annual reports look like duplicates (same
                boilerplate opening). Windowed, on our corpus (cluster run, Batch B):
                distinct documents reach at most 0.956 (2023 rules vs prospectus,
                student handbook vs rules handbook), the reworded copy b05 scores 0.995,
                so the threshold is 0.96: a thin margin on the low side, which we
                report (eval/results/near-dup-calibration.txt).

Decisions:
  quality, completeness or uniqueness fails  -> quarantine (moved out, never indexed)
  only recency fails                         -> archive (indexed with superseded=true,
                                                excluded from retrieval, restorable)
  everything passes                          -> index as the active version

What it does NOT check: whether the text contains instructions aimed at the model
(prompt injection). That is caught at query time by api/app/services/guardrail.py,
where the action is logged into the trace (A03 D4).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Document

MIN_DOC_CHARS = 200            # a whole document with less text than this is "empty"
MIN_PAGE_CHARS = 20            # a page with less is empty (after OCR)
MAX_EMPTY_PAGE_SHARE = 0.5     # more than half the pages empty: extraction failed
MIN_LETTER_SHARE = 0.60        # letters / non-space characters; garbled text is lower
MIN_COMMON_WORD_SHARE = 0.05   # share of tokens that are common English words
MAX_REPLACEMENT_SHARE = 0.01   # U+FFFD per character: a failed decode
NEAR_DUP_WINDOWS = 6           # windows spread over the document, embedded and averaged
NEAR_DUP_WINDOW_WORDS = 250    # words per window

COMMON_WORDS = set(["a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "for", "from", "has", "have", "if", "in", "into", "is", "it", "its", "may", "must", "no", "not", "of", "on", "or", "shall", "should", "such", "that", "the", "their", "there", "these", "this", "to", "under", "was", "were", "which", "will", "with", "within", "who", "student", "students", "course", "courses", "semester", "university"])

CHECKS = ("quality", "completeness", "recency", "uniqueness")


@dataclass
class Verdict:
    doc: Document
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision: str = ""                 # index | archive | quarantine

    def fail(self, check: str, reason: str, **detail: Any) -> None:
        self.checks[check] = {"pass": False, "reason": reason, **detail}

    def ok(self, check: str, reason: str = "ok", **detail: Any) -> None:
        self.checks.setdefault(check, {"pass": True, "reason": reason, **detail})

    def skip(self, check: str, reason: str) -> None:
        """Not run, because an earlier check already failed (shown as n/a)."""
        self.checks[check] = {"pass": None, "reason": f"not checked: {reason}"}

    @property
    def passed(self) -> bool:
        return all(c["pass"] is True for c in self.checks.values())

    def row(self) -> dict[str, Any]:
        d = self.doc
        return {
            "doc_id": d.doc_id, "source": d.source, "batch": d.batch, "title": d.title,
            "doc_version": d.doc_version, "family": d.family, "sha256": d.sha256,
            "pages": len(d.pages), "checks": {k: self.checks[k] for k in CHECKS},
            "decision": self.decision,
        }


def version_key(doc: Document) -> tuple[int, int, str]:
    """Order versions: '2026-06' > '2023-08', '2027-28' > '2026-27'. Falls back to
    the date, then the name, so the order is always deterministic."""
    text = doc.doc_version or doc.date
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    year = next((n for n in numbers if 1900 <= n <= 2100), 0)
    rest = [n for n in numbers if n != year]
    month = rest[0] if rest and 1 <= rest[0] <= 12 else 0
    return year, month, doc.source


# ------------------------------------------------------------------ the checks

def check_quality(v: Verdict) -> None:
    doc = v.doc
    if doc.extract_error:
        v.fail("quality", f"extraction failed: {doc.extract_error}")
        return
    text = "\n".join(p.text for p in doc.pages)
    chars = len(text.strip())
    if chars < MIN_DOC_CHARS:
        v.fail("quality", f"empty: {chars} characters of text (minimum {MIN_DOC_CHARS})",
               characters=chars)
        return
    empty = [p.page for p in doc.pages if len(p.text.strip()) < MIN_PAGE_CHARS]
    if doc.pages and len(empty) / len(doc.pages) > MAX_EMPTY_PAGE_SHARE:
        v.fail("quality", f"{len(empty)} of {len(doc.pages)} pages have no text "
               "(scanned pages that OCR could not read?)", empty_pages=empty[:20])
        return
    visible = [c for c in text if not c.isspace()]
    letters = sum(c.isalpha() for c in visible) / max(1, len(visible))
    replacement = text.count("�") / max(1, chars)
    tokens = re.findall(r"[a-z]+", text.lower())
    common = sum(t in COMMON_WORDS for t in tokens) / max(1, len(tokens))
    detail = {"letter_share": round(letters, 3), "common_word_share": round(common, 3),
              "characters": chars}
    if replacement > MAX_REPLACEMENT_SHARE:
        v.fail("quality", f"garbled: {replacement:.1%} replacement characters", **detail)
    elif letters < MIN_LETTER_SHARE:
        v.fail("quality", f"garbled: only {letters:.0%} of characters are letters "
               f"(minimum {MIN_LETTER_SHARE:.0%})", **detail)
    elif common < MIN_COMMON_WORD_SHARE:
        v.fail("quality", f"garbled: only {common:.1%} of words are common English words "
               f"(minimum {MIN_COMMON_WORD_SHARE:.0%})", **detail)
    else:
        v.ok("quality", "readable text", **detail)


def check_completeness(v: Verdict) -> None:
    doc = v.doc
    missing = []
    if not doc.title:
        missing.append("title")
    if not doc.source_uri:
        missing.append("source")
    if not (doc.doc_version or doc.date):
        missing.append("date or version")
    actual_pages = _page_count(doc)
    if actual_pages is None and doc.declared_pages is None:
        missing.append("page count")
    if missing:
        v.fail("completeness", "missing metadata: " + ", ".join(missing), missing=missing)
        return
    if doc.path.lower().endswith(".pdf") and not _pdf_has_eof(Path(doc.path)):
        v.fail("completeness", "truncated: the PDF ends before its %%EOF marker")
        return
    expected = doc.declared_pages or actual_pages
    if expected and doc.pages and len(doc.pages) < expected:
        v.fail("completeness", f"truncated: {len(doc.pages)} of {expected} pages extracted",
               expected_pages=expected, extracted_pages=len(doc.pages))
        return
    if doc.declared_pages and actual_pages and actual_pages < doc.declared_pages:
        v.fail("completeness", f"truncated: the file has {actual_pages} pages, the metadata "
               f"declares {doc.declared_pages}")
        return
    v.ok("completeness", "title, source, version/date and page count present; not truncated",
         pages=expected)


def check_uniqueness(v: Verdict, kept: list[Verdict], vectors: dict[str, list[float]],
                     threshold: float) -> None:
    doc = v.doc
    for other in kept:
        if other.doc.sha256 == doc.sha256:
            v.fail("uniqueness", f"exact duplicate of {other.doc.source} "
                   f"({other.doc.batch}), same SHA-256", duplicate_of=other.doc.doc_id)
            return
    mine = vectors.get(doc.doc_id)
    best: tuple[float, Verdict | None] = (0.0, None)
    if mine is not None:
        for other in kept:
            if other.doc.family == doc.family:        # versions are recency's job
                continue
            theirs = vectors.get(other.doc.doc_id)
            if theirs is not None:
                score = cosine(mine, theirs)
                if score > best[0]:
                    best = (score, other)
    score, closest = best
    if closest is not None and score >= threshold:
        v.fail("uniqueness", f"near-duplicate of {closest.doc.source} ({closest.doc.batch}): "
               f"similarity {score:.3f} >= {threshold}", duplicate_of=closest.doc.doc_id,
               similarity=round(score, 4))
        return
    v.ok("uniqueness", "no duplicate" + (f" (closest {closest.doc.doc_id} at {score:.3f})"
                                          if closest is not None else ""),
         closest_similarity=round(score, 4))


def check_recency(survivors: list[Verdict], pins: dict[str, str]) -> None:
    """Within each family, the newest version (or the pinned one, after a rollback)
    passes; every other version fails with the name of the newer one."""
    families: dict[str, list[Verdict]] = {}
    for v in survivors:
        families.setdefault(v.doc.family, []).append(v)
    for family, members in families.items():
        pinned = pins.get(family)
        winner = next((m for m in members if m.doc.doc_id == pinned), None) or \
            max(members, key=lambda m: version_key(m.doc))
        for m in members:
            if m is winner:
                if m.doc.superseded and len(members) == 1:
                    m.fail("recency", "declared superseded in its metadata")
                else:
                    reason = "newest version" if len(members) > 1 else "only version"
                    if pinned == m.doc.doc_id:
                        reason = "pinned by a restore (rollback)"
                    m.ok("recency", reason)
            else:
                m.fail("recency", f"newer version found: {winner.doc.source} "
                       f"(version {winner.doc.doc_version or winner.doc.date})",
                       newer=winner.doc.doc_id)


# ------------------------------------------------------------------ the stage

def verify(docs: list[Document], embed_docs: Callable[[list[Document]], dict[str, list[float]]],
           pins: dict[str, str] | None = None, threshold: float = 0.96) -> list[Verdict]:
    """Run the four checks over every document, in a deterministic order (the base
    batch first, then other batches by name, then file name), so 'the first copy wins'
    is reproducible."""
    order = sorted(docs, key=lambda d: (d.batch != "base", d.batch, d.source))
    verdicts = [Verdict(d) for d in order]
    for v in verdicts:
        check_quality(v)
        check_completeness(v)
    readable = [v for v in verdicts if v.checks["quality"]["pass"]]
    vectors = embed_docs([v.doc for v in readable]) if readable else {}
    kept: list[Verdict] = []
    for v in verdicts:
        if not v.checks["quality"]["pass"]:
            v.skip("uniqueness", "quality failed")
            continue
        check_uniqueness(v, kept, vectors, threshold)
        if v.passed:
            kept.append(v)
    check_recency(kept, pins or {})
    for v in verdicts:
        if "recency" not in v.checks:
            v.skip("recency", "failed an earlier check")
        hard_fail = any(v.checks[c]["pass"] is False
                        for c in ("quality", "completeness", "uniqueness"))
        v.decision = "quarantine" if hard_fail else \
            ("archive" if not v.checks["recency"]["pass"] else "index")
        if v.decision == "archive":
            v.doc.superseded = True
        elif v.decision == "index":
            v.doc.superseded = False
    return verdicts


def near_dup_windows(doc: Document) -> list[str]:
    """NEAR_DUP_WINDOWS evenly spaced windows of the text; their mean vector is the
    document vector for the near-duplicate check."""
    words = " ".join(p.text for p in doc.pages).split()
    size, n = NEAR_DUP_WINDOW_WORDS, NEAR_DUP_WINDOWS
    if len(words) <= size:
        return [" ".join(words)]
    starts = sorted({int(i * (len(words) - size) / (n - 1)) for i in range(n)})
    return [" ".join(words[s:s + size]) for s in starts]


def mean_vector(vectors: list[list[float]]) -> list[float]:
    return [sum(col) / len(vectors) for col in zip(*vectors)]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _page_count(doc: Document) -> int | None:
    if doc.path.lower().endswith(".pdf"):
        from .lake import pdf_page_count

        return pdf_page_count(Path(doc.path))
    return 1 if doc.path else None          # HTML, Markdown and text are one page


def _pdf_has_eof(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2048))
            return b"%%EOF" in f.read()
    except OSError:
        return False


def summary_table(verdicts: list[Verdict]) -> str:
    """The verification report as a terminal table (for the A1 screenshot)."""
    lines = [f"{'document':<46} {'batch':<10} " + " ".join(f"{c:<12}" for c in CHECKS) +
             " decision"]
    for v in verdicts:
        marks = {True: "PASS", False: "FAIL", None: "n/a"}
        cells = " ".join(f"{marks[v.checks[c]['pass']]:<12}" for c in CHECKS)
        lines.append(f"{v.doc.source[:46]:<46} {v.doc.batch[:10]:<10} {cells} {v.decision}")
        for c in CHECKS:
            if v.checks[c]["pass"] is False:
                lines.append(f"    - {c}: {v.checks[c]['reason']}")
    return "\n".join(lines)
