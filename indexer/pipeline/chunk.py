"""Stage 3: Chunk. Structure-aware, with overlap.

How it works, in three steps:

1. Walk the lines and track the current heading path, e.g.
   "5. Academic Assessment > Academic Warning/Probation". That path is the chunk's
   `section`, which citations and recall@k both use.
2. Inside a section, group lines into blocks: a block starts at a clause number
   ("5.4 ..."), a list item ("a)", "1)"), a table row or a blank line.
3. Pack whole blocks into chunks of up to `size` words. A block that is too big is
   split at sentence ends. Each new chunk in the same section starts with the last
   `overlap` words of the previous one.

Unit: words (whitespace-separated tokens). For English prose that is about 0.75 of a
model token, so CHUNK_SIZE=400 is roughly 530 model tokens; see docs/PIPELINE.md.

`chunk_fixed` is the naive baseline used in docs/FAILURE-ANALYSIS.md (stage B):
fixed windows of words that ignore headings, paragraphs and sentences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Chunk, Document

# "XII. Policy on Generative AI Based Tools"  (Roman numeral: top level)
_ROMAN = re.compile(r"^([IVXL]+)\.\s+([A-Z].{2,80})$")
# "5. Academic Assessment", "4. Eligibility criteria for a technical start-up"
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+([A-Z].{2,80})$")
# "## Fee Refund Policy" (HTML and Markdown headings)
_MARKDOWN = re.compile(r"^#{1,4}\s+(.+)$")
# A new block starts at a clause number, a list item or a table row.
_BLOCK_START = re.compile(r"^(\d+(\.\d+)+\.?\s|[a-z]\)\s|\(?[ivx]+\)\s|\d+\)\s|.+ \| )")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
                "or", "the", "to", "with", "under", "between", "into", "per"}


@dataclass
class Block:
    section: str
    page: int
    text: str


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Structure-aware chunks: sections, then paragraphs, then sentences."""
    chunks: list[Chunk] = []
    for section, blocks in _group_by_section(_blocks(doc)):
        previous_tail: list[str] = []
        current: list[str] = []
        page = blocks[0].page
        for block in blocks:
            for piece in _fit(block.text, size):
                words = piece.split()
                if current and len(current) + len(words) > size:
                    chunks.append(_make(doc, len(chunks), section, page, previous_tail + current))
                    previous_tail = current[-overlap:] if overlap else []
                    current, page = [], block.page
                if not current:
                    page = block.page
                current.extend(words)
        if current:
            chunks.append(_make(doc, len(chunks), section, page, previous_tail + current))
    return chunks


def chunk_fixed(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Naive fixed windows of `size` words, ignoring all structure (baseline only)."""
    words: list[tuple[str, int, str]] = []          # (word, page, section)
    for block in _blocks(doc):
        words.extend((w, block.page, block.section) for w in block.text.split())
    step = max(1, size - overlap)
    chunks: list[Chunk] = []
    for start in range(0, len(words), step):
        window = words[start:start + size]
        if not window:
            break
        _, page, section = window[0]
        chunks.append(Chunk(id=f"{_slug(doc.source)}-{len(chunks):04d}",
                            text=" ".join(w for w, _, _ in window), source=doc.source,
                            section=section, page=page, doc_version=doc.doc_version,
                            superseded=doc.superseded))
        if start + size >= len(words):
            break
    return chunks


# ------------------------------------------------------------------ helpers

def heading_level(line: str) -> tuple[int, str] | None:
    """Return (level, heading text) if the line is a heading, else None.

    Level 0: Roman numeral chapters. Level 1: "N. Title" and Markdown/HTML headings.
    Level 2: short Title Case lines such as "Attendance Requirements".
    """
    line = line.strip()
    if not line or len(line) > 90:
        return None
    if m := _MARKDOWN.match(line):
        return 1, m.group(1).strip()
    if line.endswith((".", ",", ";", ":")) or re.search(r"\d$", line):
        return None                                   # sentences and TOC lines
    if m := _ROMAN.match(line):
        return 0, line
    if (m := _NUMBERED.match(line)) and len(line.split()) <= 10:
        return 1, line
    if looks_like_title(line):
        return 2, line
    return None


def looks_like_title(line: str) -> bool:
    """'Withdrawal from a Course' yes; 'Department must be informed and' no."""
    words = line.split()
    # Starts with a capital, or with an ordinal such as "76th Convocation ...".
    starts_ok = line[0].isupper() or bool(re.match(r"\d+(st|nd|rd|th)\b", line))
    if not 1 <= len(words) <= 7 or not starts_ok or "|" in line:
        return False
    if re.match(r"^(\d+(\.\d+)+|[a-z]\)|\d+\))", line):
        return False
    content = [w for w in words if w.lower() not in _SMALL_WORDS]
    capitals = sum(1 for w in content if w[0].isupper() or w[0].isdigit())
    return bool(content) and capitals / len(content) >= 0.75 and len(words) >= 2


def _blocks(doc: Document) -> list[Block]:
    """Split the document into blocks, each tagged with its heading path and page."""
    path = ["", "", ""]                               # level 0, 1, 2 headings
    blocks: list[Block] = []
    lines: list[str] = []
    page_of_block = 1

    def section() -> str:
        return " > ".join(p for p in path if p) or "Introduction"

    def flush() -> None:
        nonlocal lines
        text = " ".join(lines).strip()
        if text:
            blocks.append(Block(section(), page_of_block, text))
        lines = []

    for page in doc.pages:
        for raw in page.text.splitlines():
            line = raw.strip()
            if not line:
                flush()
                continue
            found = heading_level(line)
            if found:
                flush()
                level, title = found
                path[level] = title
                for deeper in range(level + 1, 3):
                    path[deeper] = ""
                continue
            if _BLOCK_START.match(line):
                flush()
            if not lines:
                page_of_block = page.page
            lines.append(line)
        # a page break does not end a block: clauses often continue on the next page
    flush()
    return blocks


def _group_by_section(blocks: list[Block]) -> list[tuple[str, list[Block]]]:
    groups: list[tuple[str, list[Block]]] = []
    for block in blocks:
        if groups and groups[-1][0] == block.section:
            groups[-1][1].append(block)
        else:
            groups.append((block.section, [block]))
    return groups


def _fit(text: str, size: int) -> list[str]:
    """Return text as pieces of at most `size` words, cutting at sentence ends first."""
    if len(text.split()) <= size:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        words = sentence.split()
        if current and len(current) + len(words) > size:
            pieces.append(" ".join(current))
            current = []
        while len(words) > size:                      # one enormous "sentence"
            pieces.append(" ".join(words[:size]))
            words = words[size:]
        current.extend(words)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _make(doc: Document, n: int, section: str, page: int, words: list[str]) -> Chunk:
    # The section path is written into the text too: the embedding then "knows" which
    # rule a clause belongs to, even when the clause itself never names it.
    return Chunk(id=f"{_slug(doc.source)}-{n:04d}", text=f"{section}\n{' '.join(words)}",
                 source=doc.source, section=section, page=page,
                 doc_version=doc.doc_version, superseded=doc.superseded)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
