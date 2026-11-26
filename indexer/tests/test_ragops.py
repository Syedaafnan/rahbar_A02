"""A03 (RAGOps) data management: verification, quarantine, incremental updates,
deletion without orphans, archiving and restore. No Chroma and no model: a fake
collection and a bag-of-words "embedding" stand in for them."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import get_settings
from pipeline.lake import Registry
from pipeline.models import Document, Page
from pipeline.run import ingest, print_count_by_doc, restore
from pipeline.verify import Verdict, check_quality, verify, version_key

# ------------------------------------------------------------------ fakes


class FakeCollection:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
        self.rows: dict[str, list] = {}

    def upsert(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[no-untyped-def]
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = [d, e, dict(m)]

    def update(self, ids, metadatas) -> None:  # type: ignore[no-untyped-def]
        for i, m in zip(ids, metadatas):
            self.rows[i][2] = dict(m)

    def get(self, where=None, include=None) -> dict:  # type: ignore[no-untyped-def]
        ids = [i for i, r in self.rows.items()
               if not where or all(r[2].get(k) == v for k, v in where.items())]
        return {"ids": ids, "metadatas": [dict(self.rows[i][2]) for i in ids]}

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

    def get_collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            raise ValueError("not found")
        return self.collections[name]

    def create_collection(self, name: str, metadata: dict) -> FakeCollection:
        self.collections[name] = FakeCollection(metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        del self.collections[name]


class FakeEmbedder:
    """Bag-of-words vectors: near-identical texts score close to 1, unrelated ones low.
    Counts how many texts it was asked to embed."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [0.0] * 128
            for w in re.findall(r"[a-z]{4,}", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 128] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of text per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        stream = f"BT /F1 11 Tf 50 750 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                    f"<< /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    body = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    body += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


LIBRARY = """# Library Borrowing Rules

## Borrowing Limits
Undergraduate students may borrow four books at a time for fourteen days. Graduate
students may borrow six books for twenty one days. A borrowed book can be renewed twice
if nobody has reserved it.

## Overdue Fines
The fine for an overdue book is fifty rupees per day per book. Borrowing privileges are
suspended when unpaid fines exceed one thousand rupees.
"""

LAB = """# Laboratory Safety Policy

## Protective Equipment
Students working in chemistry and electronics laboratories must wear safety goggles and
closed shoes. Food and drinks are never allowed inside any laboratory.

## Reporting Incidents
Every accident, spill or electrical fault must be reported to the lab engineer within
one hour, and recorded in the incident register kept at the laboratory entrance.
"""

SPORTS = """Sports Complex Timings

The sports complex opens at seven in the morning and closes at ten at night. Cricket
nets must be booked through the sports office one day in advance. Swimming pool access
needs a medical certificate renewed every semester, and guests are not permitted.
"""


def _write_batch(folder: Path, docs: dict[str, tuple[str | bytes, dict[str, Any] | None]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, (content, meta) in docs.items():
        target = folder / name
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        if meta is not None:
            manifest.append({"file": name, **meta})
    import yaml
    (folder / "_batch.yaml").write_text(yaml.safe_dump({"documents": manifest}), encoding="utf-8")


def _meta(title: str, version: str = "2026", family: str | None = None, **extra: Any) -> dict:
    m = {"title": title, "source": f"test://{title}", "doc_version": version, **extra}
    if family:
        m["family"] = family
    return m


@pytest.fixture()
def lake(tmp_path: Path) -> dict[str, Any]:
    data = tmp_path / "data"
    raw = data / "lake" / "raw"
    _write_batch(raw / "base", {
        "library-rules.md": (LIBRARY, _meta("Library Rules", "2025", family="library")),
        "lab-safety.md": (LAB, _meta("Lab Safety")),
    })
    settings = replace(get_settings(), data_dir=str(data), corpus_dir=str(raw),
                       near_dup_threshold=0.9, chunk_size=40, chunk_overlap=5)
    return {"settings": settings, "raw": raw, "data": data, "client": FakeClient(),
            "embed": FakeEmbedder()}


def _run(lake: dict[str, Any]) -> dict[str, Any]:
    return ingest(lake["settings"], client=lake["client"], embed_fn=lake["embed"])


def _rows(lake: dict[str, Any]) -> dict[str, list]:
    return lake["client"].collections["rahbar"].rows


# ------------------------------------------------------------------ A2: incremental


def test_second_run_on_unchanged_corpus_embeds_zero_chunks(lake: dict[str, Any]) -> None:
    first = _run(lake)
    assert first["embedded"] > 0 and first["quarantined"] == 0
    calls = lake["embed"].calls
    second = _run(lake)
    assert second["embedded"] == 0
    assert second["skipped_unchanged"] == first["chunks_wanted"]
    assert second["deleted"] == 0 and second["metadata_updated"] == 0
    assert lake["embed"].calls == calls          # not even the document vectors
    assert second["index_version"] == first["index_version"]   # cache stays valid


def test_changed_document_replaces_only_its_own_chunks(lake: dict[str, Any]) -> None:
    _run(lake)
    lab_before = {i for i, r in _rows(lake).items() if r[2]["doc_id"] == "lab-safety-md"}
    path = lake["raw"] / "base" / "library-rules.md"
    path.write_text(LIBRARY.replace("fifty rupees", "one hundred rupees"), encoding="utf-8")
    result = _run(lake)
    assert set(result["embedded_by_doc"]) == {"library-rules-md"}
    assert set(result["deleted_by_doc"]) == {"library-rules-md"}
    assert result["embedded"] == result["deleted"] >= 1
    lab_after = {i for i, r in _rows(lake).items() if r[2]["doc_id"] == "lab-safety-md"}
    assert lab_after == lab_before


def test_deleted_document_leaves_no_orphans(lake: dict[str, Any],
                                            capsys: pytest.CaptureFixture[str]) -> None:
    _run(lake)
    (lake["raw"] / "base" / "lab-safety.md").unlink()
    result = _run(lake)
    assert result["deleted_documents"] == ["lab-safety-md"]
    assert not [r for r in _rows(lake).values() if r[2]["doc_id"] == "lab-safety-md"]
    assert print_count_by_doc(lake["settings"], client=lake["client"]) == 0   # no orphans
    assert "0 orphaned documents" in capsys.readouterr().out


# ------------------------------------------------------------------ A1: verification


def test_batch_with_planted_problems(lake: dict[str, Any]) -> None:
    _run(lake)
    near = LIBRARY.replace("Undergraduate students", "Undergraduates").replace(
        "is fifty rupees", "is 50 rupees")
    _write_batch(lake["raw"] / "batch_b", {
        "library-copy.md": (LIBRARY, _meta("Library Rules copy")),             # exact dup
        "library-reworded.md": (near, _meta("Library Rules reworded")),        # near dup
        "library-rules-2026.md": (LIBRARY.replace("four books", "five books"),
                                  _meta("Library Rules 2026", "2026", family="library")),
        "no-metadata.md": (SPORTS, None),                                      # no manifest
        "truncated.pdf": (make_pdf(["Page one text about fees"] * 3)[:-120],
                          _meta("Transport", pages=3)),
        "garbled.txt": ("#@%$ ^&*( ){} 0x9f 0x3a ~~ ##@ " * 40, _meta("Scan")),
    })
    summary = _run(lake)
    reports = sorted((lake["data"] / "reports").glob("verify-*.json"),
                     key=lambda f: f.stat().st_mtime_ns)
    rows = {r["source"]: r for r in json.loads(reports[-1].read_text(encoding="utf-8"))["documents"]}

    def failed(src: str) -> set[str]:
        return {c for c, r in rows[src]["checks"].items() if r["pass"] is False}

    assert failed("library-copy.md") == {"uniqueness"}
    assert "exact duplicate" in rows["library-copy.md"]["checks"]["uniqueness"]["reason"]
    assert failed("library-reworded.md") == {"uniqueness"}
    assert "near-duplicate" in rows["library-reworded.md"]["checks"]["uniqueness"]["reason"]
    assert failed("no-metadata.md") == {"completeness"}
    assert "source" in rows["no-metadata.md"]["checks"]["completeness"]["reason"]
    assert "completeness" in failed("truncated.pdf") or "quality" in failed("truncated.pdf")
    assert failed("garbled.txt") == {"quality"}
    # the older library version is archived, the 2026 one is active
    assert failed("library-rules.md") == {"recency"}
    assert rows["library-rules.md"]["decision"] == "archive"
    assert rows["library-rules-2026.md"]["decision"] == "index"
    assert summary["quarantined"] == 5 and summary["archived"] == 1
    # quarantined files moved out of the lake, with a reason file next to them
    quarantined = list((lake["data"] / "lake" / "quarantine").rglob("*.reason.json"))
    assert len(quarantined) == 5
    assert not (lake["raw"] / "batch_b" / "garbled.txt").exists()
    # archived chunks stay in the store, flagged, and a copy of the file is archived
    old = [r for r in _rows(lake).values() if r[2]["doc_id"] == "library-rules-md"]
    assert old and all(r[2]["superseded"] and r[2]["status"] == "archived" for r in old)
    assert list((lake["data"] / "lake" / "archive" / "library-rules-md").rglob("*.md"))


def test_restore_rolls_back_to_the_archived_version(lake: dict[str, Any]) -> None:
    _write_batch(lake["raw"] / "batch_b", {
        "library-rules-2026.md": (LIBRARY.replace("four books", "five books"),
                                  _meta("Library Rules 2026", "2026", family="library")),
    })
    _run(lake)
    restore(lake["settings"], "library-rules-md", client=lake["client"])
    status = {r[2]["doc_id"]: r[2]["status"] for r in _rows(lake).values()}
    assert status["library-rules-md"] == "active"
    assert status["library-rules-2026-md"] == "archived"
    # the pin survives the next scheduled ingest, and nothing is re-embedded
    again = _run(lake)
    assert again["embedded"] == 0
    status = {r[2]["doc_id"]: r[2]["status"] for r in _rows(lake).values()}
    assert status["library-rules-md"] == "active"
    reg = Registry(Path(lake["settings"].data_dir) / "state" / "rahbar" / "registry.json")
    assert reg.pins == {"library": "library-rules-md"}


def test_changed_document_that_fails_keeps_its_last_good_version(lake: dict[str, Any]) -> None:
    _run(lake)
    before = {i for i, r in _rows(lake).items() if r[2]["doc_id"] == "lab-safety-md"}
    (lake["raw"] / "base" / "lab-safety.md").write_text("too short", encoding="utf-8")
    result = _run(lake)
    assert result["quarantined"] == 1 and result["kept"] == len(before)
    after = {i for i, r in _rows(lake).items() if r[2]["doc_id"] == "lab-safety-md"}
    assert after == before


def test_quality_check_flags_empty_scans() -> None:
    doc = Document("scan.pdf", "2026", False, [Page("scan.pdf", i, "") for i in range(1, 5)])
    v = Verdict(doc)
    check_quality(v)
    assert not v.checks["quality"]["pass"] and "empty" in v.checks["quality"]["reason"]


def test_versions_order_by_year_then_month() -> None:
    def d(v: str) -> Document:
        return Document("x", v, False)
    assert version_key(d("2026-06")) > version_key(d("2023-08"))
    assert version_key(d("2027-28")) > version_key(d("2026-27"))
    assert version_key(d("2026-06")) > version_key(d("2026-01"))


def test_verify_without_vectors_still_checks_exact_duplicates() -> None:
    text = LIBRARY * 2
    a = Document("a.md", "2026", False, [Page("a.md", 1, text)], title="A", source_uri="x",
                 sha256="same", path="a.md", batch="base")
    b = replace(a, source="b.md", doc_id="", family="", path="b.md")
    b.__post_init__()
    verdicts = verify([a, b], lambda docs: {}, threshold=0.95)
    assert [v.decision for v in verdicts] == ["index", "quarantine"]


def test_fixture_ingest_without_a_lake(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
    corpus = tmp_path / "fixtures"
    shutil.copytree(fixtures, corpus)
    settings = replace(get_settings(), corpus_dir=str(corpus), data_dir="")
    client = FakeClient()
    result = ingest(settings, client=client, embed_fn=FakeEmbedder())
    assert result["quarantined"] == 0 and result["archived"] == 1
    rows = client.collections["rahbar"].rows.values()
    assert {r[2]["source"] for r in rows if r[2]["superseded"]} == {"academic-rules-2023.md"}
