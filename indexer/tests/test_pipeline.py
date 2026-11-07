"""Unit tests for the five indexer stages. No network, no model, no Chroma server:
OCR is not exercised here (it needs Tesseract), Ollama is a fake transport and
Chroma is a fake client."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from pipeline.chunk import chunk, chunk_fixed, heading_level
from pipeline.clean import clean, clean_text
from pipeline.config import get_settings
from pipeline.embed import DOCUMENT_PREFIX, embed
from pipeline.extract import extract, find_gutter, html_to_text, strip_front_matter
from pipeline.models import Chunk, Document, Page
from pipeline.run import build_chunks
from pipeline.store import store

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
REQUIRED_METADATA = {"source", "section", "page", "doc_version", "superseded"}


# ------------------------------------------------------------------ extract

def test_markdown_front_matter_is_removed() -> None:
    text = "---\ndoc_version: 2026\nsuperseded: false\n---\n# Title\nBody"
    assert strip_front_matter(text) == "# Title\nBody"


def test_html_keeps_headings_and_table_rows_and_drops_menus() -> None:
    html = """<nav><a>Home</a><a>Admissions</a></nav>
      <h2>Fee Refund Policy</h2><p>Refunds depend on the date.</p>
      <table><tr><th>Timeline</th><th>Refund</th></tr>
             <tr><td>Upto 10th day</td><td>100%</td></tr></table>
      <footer>Copyright</footer>"""
    text = html_to_text(html)
    assert text.splitlines()[0] == "## Fee Refund Policy"
    assert "Upto 10th day | 100%" in text
    assert "Home" not in text and "Copyright" not in text


def test_gutter_found_between_two_columns_only() -> None:
    two_columns = [(40 + i % 5, 200) for i in range(30)] + [(320, 500 - i % 5) for i in range(30)]
    middle = find_gutter(two_columns, 0, 612)
    assert middle is not None and 200 < middle < 320
    one_column = [(40, 560)] * 60                      # every line spans the page
    assert find_gutter(one_column, 0, 612) is None


def test_fixture_markdown_extracts_as_one_page() -> None:
    doc = extract(str(FIXTURES / "academic-rules-2026.md"), "2026", False)
    assert doc.source == "academic-rules-2026.md"
    assert len(doc.pages) == 1 and not doc.pages[0].ocr
    assert "superseded:" not in doc.pages[0].text


# -------------------------------------------------------------------- clean

def test_clean_rejoins_letter_spacing_and_split_decimals() -> None:
    assert clean_text("labs, t u t o r i a l s and") == "labs, tutorials and"
    assert clean_text("threshold of 2. 00 for") == "threshold of 2.00 for"
    assert clean_text("conﬁrmed") == "confirmed"              # ligature


def test_clean_drops_running_headers_but_keeps_clauses() -> None:
    header = "NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES"
    pages = [Page("d.pdf", n, f"{header}\n5.{n} Clause number {n}.\nPage {n} of 6")
             for n in range(1, 7)]
    cleaned = clean(Document("d.pdf", "2026", False, pages))
    for n, page in enumerate(cleaned.pages, start=1):
        assert header not in page.text
        assert "Page" not in page.text
        assert f"5.{n} Clause number {n}." in page.text


# -------------------------------------------------------------------- chunk

@pytest.mark.parametrize("line, level", [
    ("XII. Policy on Generative AI Based Tools", 0),
    ("5. Academic Assessment", 1),
    ("## Fee Refund Policy", 1),
    ("Withdrawal from a Course", 2),
    ("76th Convocation of the University", 2),
])
def test_headings_are_recognised(line: str, level: int) -> None:
    found = heading_level(line)
    assert found is not None and found[0] == level


@pytest.mark.parametrize("line", [
    "5.4 Minimum CGPA required to avoid academic warning is 2.00 for all",
    "Department must be informed and formal permission",
    "Withdrawal from a Course 16",                    # a table-of-contents line
    "The request should be made within the first two weeks.",
])
def test_body_lines_are_not_headings(line: str) -> None:
    assert heading_level(line) is None


def _doc(text: str) -> Document:
    return Document("rules.pdf", "2026", False, [Page("rules.pdf", 1, text)])


def test_chunks_carry_section_path_and_required_metadata() -> None:
    text = ("5. Academic Assessment\nAcademic Warning/Probation\n"
            "5.4 Minimum CGPA required to avoid academic warning is 2.00.\n"
            "5.5 If CGPA drops below 2.00, warning is issued.\n"
            "Repeating a Course\n5.9 A student may repeat any course to improve CGPA.")
    chunks = chunk(_doc(text), size=100, overlap=10)
    sections = [c.section for c in chunks]
    assert sections == ["5. Academic Assessment > Academic Warning/Probation",
                        "5. Academic Assessment > Repeating a Course"]
    assert "5.4 Minimum CGPA" in chunks[0].text and "5.9" not in chunks[0].text
    for c in chunks:
        assert set(c.metadata()) == REQUIRED_METADATA
        assert c.text.startswith(c.section)           # section header is embedded too


def test_long_sections_are_split_under_size_with_overlap() -> None:
    clauses = "\n".join(f"1.{i} " + " ".join(f"w{i}x{j}" for j in range(30)) + "."
                        for i in range(1, 11))
    chunks = chunk(_doc("Attendance Requirements\n" + clauses), size=100, overlap=20)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text.split()) <= 100 + 20 + 2    # body + overlap + section header
    first_tail = chunks[0].text.split()[-20:]
    assert chunks[1].text.split()[2:22] == first_tail  # the overlap carries over


def test_chunk_ids_are_stable_and_unique() -> None:
    doc = _doc("Attendance Requirements\n1.21 Students must attend.\n1.22 Or fail.")
    first = [c.id for c in chunk(doc, 50, 5)]
    assert first == [c.id for c in chunk(doc, 50, 5)]
    assert len(set(first)) == len(first)


def test_fixed_chunker_ignores_structure() -> None:
    text = "Heading One\n" + " ".join(f"word{i}" for i in range(250))
    pieces = chunk_fixed(_doc(text), size=100, overlap=0)
    assert [len(p.text.split()) for p in pieces] == [100, 100, 50]


def test_fixture_corpus_chunks_with_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPUS_DIR", str(FIXTURES))
    chunks = build_chunks(get_settings())
    by_source = {c.source: c for c in chunks}
    assert by_source["academic-rules-2023.md"].superseded is True
    assert by_source["academic-rules-2026.md"].superseded is False
    assert any(c.section == "1. Attendance" and c.source == "academic-rules-2026.md"
               for c in chunks)


# -------------------------------------------------------------------- embed

def test_embed_batches_and_uses_the_pinned_model_and_prefix() -> None:
    seen: list[dict] = []

    def fake_ollama(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]] * len(body["input"])})

    settings = replace(get_settings(), embed_model="pinned-model")
    vectors = embed([f"text {i}" for i in range(20)], settings,
                    transport=httpx.MockTransport(fake_ollama))
    assert len(vectors) == 20
    assert [len(b["input"]) for b in seen] == [16, 4]
    assert all(b["model"] == "pinned-model" for b in seen)
    assert seen[0]["input"][0] == DOCUMENT_PREFIX + "text 0"


# -------------------------------------------------------------------- store

class FakeCollection:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
        self.rows: dict[str, tuple] = {}

    def upsert(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[no-untyped-def]
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = (d, e, m)

    def get(self, include=None) -> dict:  # type: ignore[no-untyped-def]
        return {"ids": list(self.rows)}

    def delete(self, ids) -> None:  # type: ignore[no-untyped-def]
        for i in ids:
            self.rows.pop(i, None)

    def modify(self, metadata: dict) -> None:
        self.metadata = {**self.metadata, **metadata}

    def count(self) -> int:
        return len(self.rows)


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.deleted = 0

    def get_collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            raise ValueError("not found")
        return self.collections[name]

    def create_collection(self, name: str, metadata: dict) -> FakeCollection:
        self.collections[name] = FakeCollection(metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        self.deleted += 1
        del self.collections[name]


def _chunks(*ids: str) -> list[Chunk]:
    return [Chunk(i, f"text {i}", "a.pdf", "1 S", 1, "2026", False) for i in ids]


def test_store_records_model_dimension_and_version() -> None:
    client = FakeClient()
    assert store(_chunks("a", "b"), [[0.1, 0.2]] * 2, get_settings(), client=client) == 2
    meta = client.collections["rahbar"].metadata
    assert meta["embed_model"] == get_settings().embed_model
    assert meta["dimension"] == 2
    assert meta["hnsw:space"] == "cosine"
    assert meta["index_version"]


def test_reindex_with_same_model_updates_in_place_and_removes_stale() -> None:
    client = FakeClient()
    store(_chunks("a", "b", "c"), [[0.1, 0.2]] * 3, get_settings(), client=client)
    store(_chunks("a", "d"), [[0.1, 0.2]] * 2, get_settings(), client=client)
    assert client.deleted == 0                         # never emptied while serving
    assert sorted(client.collections["rahbar"].rows) == ["a", "d"]


def test_reindex_with_new_model_rebuilds_the_collection() -> None:
    client = FakeClient()
    store(_chunks("a"), [[0.1, 0.2]], get_settings(), client=client)
    other = replace(get_settings(), embed_model="another-model")
    store(_chunks("a"), [[0.1, 0.2, 0.3]], other, client=client)
    assert client.deleted == 1
    assert client.collections["rahbar"].metadata["embed_model"] == "another-model"


def test_store_rejects_mismatched_vectors() -> None:
    with pytest.raises(ValueError):
        store(_chunks("a", "b"), [[0.1]], get_settings(), client=FakeClient())
