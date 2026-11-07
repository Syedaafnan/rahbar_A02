"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules, in the order they run (also listed in docs/PIPELINE.md):

1. Unicode NFKC, which turns ligatures like "ﬁ" into "fi"; the broken apostrophe
   glyph (U+FFFD) becomes "'".
2. Re-join letter-spaced words: "t u t o r i a l s" -> "tutorials" (prospectus).
3. Re-join split decimals: "2. 00" -> "2.00", so an exact-term query can match.
4. Drop lines that repeat on many pages (running headers and footers such as
   "NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES" or "Page 3 of 85").
5. Drop lines that are only a page number.

Deliberately NOT stripped: headings, clause numbers ("5.4"), list markers ("a)"),
and table rows. Chunking needs the headings for `section`, and citations and
exact-term questions need the clause numbers.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import Document, Page

# A line on at least this share of pages (and on 3+ pages) is a running header/footer.
REPEAT_SHARE = 0.4

_LETTER_SPACED = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")
_SPLIT_DECIMAL = re.compile(r"(\d)\. (\d{2})\b")
_PAGE_NUMBER = re.compile(r"^\s*(page\s*)?\d{1,3}(\s*of\s*\d{1,3})?\s*$", re.I)


def clean(doc: Document) -> Document:
    pages = [Page(p.source, p.page, clean_text(p.text), p.ocr) for p in doc.pages]
    repeated = repeated_lines(pages)
    for page in pages:
        kept = [line for line in page.text.splitlines()
                if _signature(line) not in repeated and not _PAGE_NUMBER.match(line)]
        page.text = "\n".join(kept)
    return Document(doc.source, doc.doc_version, doc.superseded, pages)


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("�", "'")
    text = _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)
    text = _SPLIT_DECIMAL.sub(r"\1.\2", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(lines)


def repeated_lines(pages: list[Page]) -> set[str]:
    """Signatures of lines that appear on many pages: running headers and footers."""
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_signature(line) for line in page.text.splitlines() if line.strip()})
    limit = max(3, int(len(pages) * REPEAT_SHARE))
    return {sig for sig, n in counts.items() if n >= limit and sig}


def _signature(line: str) -> str:
    """Lines are compared exactly (ignoring case). Digits are NOT masked: clauses such
    as '5.1 ...' and '5.2 ...' differ only in their numbers and must both survive.
    Numbered footers ('Page 3 of 85') are removed by _PAGE_NUMBER instead."""
    return line.strip().lower()
