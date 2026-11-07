"""Stage 1: Extract. PDF, HTML or Markdown to text, page by page.

What this stage handles, and why (details and examples in docs/PIPELINE.md):

- PDFs with a text layer are read with pdfplumber, keeping only the characters that
  sit INSIDE the page. The 2026 handbook stores the neighbouring page's text at
  negative x positions; without this filter every clause comes out twice.
- Multi-column pages (the prospectus) are split at the empty vertical gutter between
  columns, and each column is read top to bottom.
- Pages with no text layer (the two scanned PDFs) are rendered to an image and OCR'd
  with Tesseract. Those pages get ocr=True.
- HTML pages keep their headings (as "## Heading" lines) and their tables (as
  "cell | cell" rows), and drop menus, scripts and footers.
- Markdown (the CI fixtures) loses its front matter and keeps everything else.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path

from .models import Document, Page

# A page with fewer real characters than this is treated as "no text layer".
MIN_TEXT_CHARS = 50
OCR_DPI = 200


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page."""
    file = Path(path)
    doc = Document(source=file.name, doc_version=doc_version, superseded=superseded)
    suffix = file.suffix.lower()
    if suffix == ".pdf":
        doc.pages = _extract_pdf(file)
    elif suffix in (".html", ".htm"):
        doc.pages = [Page(source=file.name, page=1, text=html_to_text(_read(file)))]
    else:  # .md and .txt
        doc.pages = [Page(source=file.name, page=1, text=strip_front_matter(_read(file)))]
    return doc


# --------------------------------------------------------------------------- PDF

def _extract_pdf(file: Path) -> list[Page]:
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(str(file)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            visible = page.within_bbox(page.bbox)       # drop off-page text
            text = _read_columns(visible)
            if len(text.strip()) >= MIN_TEXT_CHARS or not ocr_enabled():
                pages.append(Page(source=file.name, page=number, text=text))
            else:
                pages.append(Page(source=file.name, page=number,
                                  text=ocr_page(file, number), ocr=True))
    return pages


def _read_columns(page, depth: int = 0) -> str:  # type: ignore[no-untyped-def]
    """Read a page column by column when it has an empty vertical gutter.

    Splits at the gutter, then looks inside each half again (the prospectus has three
    columns). A page with no gutter is read as one block.
    """
    words = page.extract_words()
    gutter = None
    if depth < 3:
        gutter = find_gutter([(w["x0"], w["x1"]) for w in words], page.bbox[0], page.bbox[2])
    if gutter is None:
        return page.extract_text() or ""
    x0, top, x1, bottom = page.bbox
    left = _read_columns(page.crop((x0, top, gutter, bottom)), depth + 1)
    right = _read_columns(page.crop((gutter, top, x1, bottom)), depth + 1)
    return left + "\n" + right


def find_gutter(spans: list[tuple[float, float]], page_x0: float, page_x1: float,
                min_width: float = 8.0) -> float | None:
    """Return the x position of an empty vertical band between two columns, or None.

    A band counts as a gutter only if it is at least `min_width` points wide, lies in
    the middle 80% of the page, and has at least a quarter of the words on each side.
    Ordinary single-column text never has such a band, because lines run across it.
    """
    if len(spans) < 40:
        return None
    width = page_x1 - page_x0
    covered = [False] * int(width + 1)
    for a, b in spans:
        for x in range(max(0, int(a - page_x0)), min(len(covered), int(b - page_x0) + 1)):
            covered[x] = True
    start = None
    for x in range(int(width * 0.1), int(width * 0.9)):
        if not covered[x] and start is None:
            start = x
        elif covered[x] and start is not None:
            if x - start >= min_width:
                middle = page_x0 + (start + x) / 2
                left = sum(1 for a, _ in spans if a < middle)
                if 0.25 <= left / len(spans) <= 0.75:
                    return middle
            start = None
    return None


def ocr_enabled() -> bool:
    """OCR_ENABLED=false skips OCR: scanned pages then come out empty. Only used to
    demonstrate failure mode A (docs/FAILURE-ANALYSIS.md); never in the ConfigMap."""
    return os.environ.get("OCR_ENABLED", "true").lower() != "false"


def ocr_page(file: Path, number: int) -> str:
    """Render one PDF page to an image and read it with Tesseract.

    Needs the system packages tesseract-ocr and poppler-utils (see indexer/Dockerfile).
    One page at a time keeps memory flat even for the 26-page scan.
    """
    import pytesseract
    from pdf2image import convert_from_path

    images = convert_from_path(str(file), dpi=OCR_DPI, first_page=number,
                               last_page=number, grayscale=True)
    # --psm 4: "a single column of text of variable sizes", which suits rule books.
    return "\n".join(pytesseract.image_to_string(img, config="--psm 4") for img in images)


# -------------------------------------------------------------------------- HTML

class _HtmlText(HTMLParser):
    """Collect readable text: headings as '## ...', table rows as 'a | b | c'."""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "form", "svg", "button"}
    BLOCK = {"p", "div", "li", "br", "section", "article", "ul", "ol", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.buffer: list[str] = []
        self.skip_depth = 0
        self.heading = False
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _flush(self) -> None:
        text = " ".join("".join(self.buffer).split())
        if text:
            self.lines.append(f"## {text}" if self.heading else text)
        self.buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip_depth += 1
        elif self.skip_depth:
            return
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self.heading = True
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag in self.BLOCK and self.cell is None:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif self.skip_depth:
            return
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self.heading = False
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells = [c for c in self.row if c]
            if cells:
                self.lines.append(" | ".join(cells))
            self.row = None
        elif tag in self.BLOCK and self.cell is None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buffer.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser._flush()
    lines = parser.lines
    # Site menus come before the first heading of the page content; drop them.
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), 0)
    return "\n".join(lines[first:])


# ---------------------------------------------------------------------- Markdown

def strip_front_matter(text: str) -> str:
    """Remove a leading '---' ... '---' block (doc_version, superseded)."""
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)


def _read(file: Path) -> str:
    return file.read_text(encoding="utf-8", errors="replace")
