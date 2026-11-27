"""The shapes passed between stages. Metadata fields are required (section 3.1).

A03 (RAGOps) adds document identity and provenance: every document has a stable
`doc_id`, a content hash, the metadata the verification stage checks (title, source,
date or version, page count), and a `family` that links versions of one document.
Every chunk carries its `doc_id` and the hash of its own text, which is what makes
incremental updates possible (pipeline/store.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml or the batch manifest
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)
    # --- A03: identity and provenance (filled by pipeline/lake.py) -------------
    doc_id: str = ""         # stable id: slug of the file name
    title: str = ""
    source_uri: str = ""     # where it came from: a URL, or the batch it arrived in
    family: str = ""         # versions of one document share a family
    date: str = ""           # publication date, if known (version or date is required)
    declared_pages: int | None = None   # page count from the metadata or the file itself
    sha256: str = ""         # hash of the file bytes
    path: str = ""           # where the file sits in the data lake
    batch: str = ""          # the folder (batch) it was ingested from
    extract_error: str = ""  # set when extraction failed (the quality check reads it)

    def __post_init__(self) -> None:
        if not self.doc_id:
            self.doc_id = slug(self.source)
        if not self.family:
            self.family = self.doc_id


@dataclass
class Chunk:
    id: str                  # "<doc_id>:<hash of text>": same text gives the same id
    text: str
    source: str
    section: str             # the heading this chunk sits under, e.g. "5.4 Academic Warning"
    page: int
    doc_version: str
    superseded: bool
    doc_id: str = ""
    title: str = ""
    family: str = ""
    chunk_hash: str = ""

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool."""
        return {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
            # Archived versions stay in the index (so they can be restored) but are
            # excluded from normal retrieval by the superseded/status filter.
            "status": "archived" if self.superseded else "active",
            "doc_id": self.doc_id or slug(self.source),
            "title": self.title,
            "family": self.family or self.doc_id or slug(self.source),
            "chunk_hash": self.chunk_hash,
        }
