"""The data lake on the corpus PVC (A03, paper 4.2.3): where documents live, and the
registry that remembers what was ingested.

Layout (all under DATA_DIR, the corpus-data PersistentVolumeClaim, /data in the pod):

    lake/raw/<batch>/...        incoming documents, one folder per batch
                                 base/   the FAST-NUCES corpus, written by corpus/fetch.py
                                 batch_b/ ... the planted-problems batch, and any new batch
    lake/quarantine/<run>/      documents that failed verification, with <file>.reason.json
    lake/archive/<doc_id>/<version>/   copies of superseded versions (for rollback)
    state/<collection>/registry.json   doc_id -> hash, version, status, chunk ids
    state/extract-cache/<sha256>.json  extracted + cleaned text, so unchanged documents
                                        are never OCR'd twice
    reports/verify-<ts>.json    one verification report per run
    reports/ingest-<ts>.json    one ingestion summary per run (embedded / skipped / deleted)

Metadata for each document comes from, in order of priority:
  1. the batch manifest `_batch.yaml` in the batch folder (or fetch.py's `_index.json`),
  2. a sidecar file `<file>.meta.yaml`,
  3. the document itself: Markdown front matter, the HTML <title>, the PDF info dict.
Nothing is invented: a document with no title or no date fails the completeness check.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Document, slug

SUFFIXES = {".pdf", ".md", ".html", ".htm", ".txt"}
MANIFESTS = {"_batch.yaml", "_index.json"}


@dataclass
class LakePaths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "lake" / "raw"

    @property
    def quarantine(self) -> Path:
        return self.root / "lake" / "quarantine"

    @property
    def archive(self) -> Path:
        return self.root / "lake" / "archive"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def extract_cache(self) -> Path:
        return self.root / "state" / "extract-cache"

    def state(self, collection: str) -> Path:
        return self.root / "state" / collection

    def ensure(self) -> None:
        for p in (self.raw, self.quarantine, self.archive, self.reports, self.extract_cache):
            p.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# ------------------------------------------------------------------ discovery

@dataclass
class Candidate:
    """A file found in the lake, with whatever metadata was declared for it."""

    path: Path
    batch: str
    meta: dict[str, Any] = field(default_factory=dict)
    meta_sources: list[str] = field(default_factory=list)   # where the metadata came from


def discover(corpus_dir: str | Path) -> list[Candidate]:
    """Every document under corpus_dir. Each sub-folder is a batch; files directly in
    corpus_dir form the batch named after the folder itself (the CI fixtures)."""
    root = Path(corpus_dir)
    if not root.exists():
        return []
    found: list[Candidate] = []
    folders = [root] + sorted(p for p in root.rglob("*") if p.is_dir())
    for folder in folders:
        manifest = _manifest(folder)
        batch = root.name if folder == root else folder.relative_to(root).as_posix()
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.name in MANIFESTS:
                continue
            if path.name.startswith(("_", ".")) or path.name.upper().startswith("MANIFEST"):
                continue                              # bookkeeping files, not documents
            if path.suffix.lower() not in SUFFIXES or path.name.lower().startswith("readme"):
                continue
            if path.name.endswith((".meta.yaml", ".reason.json")):
                continue
            cand = Candidate(path=path, batch=batch)
            if path.name in manifest:
                cand.meta.update({k: v for k, v in manifest[path.name].items() if v not in (None, "")})
                cand.meta_sources.append("manifest")
            sidecar = path.with_name(path.name + ".meta.yaml")
            if sidecar.exists():
                extra = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
                cand.meta.update({k: v for k, v in extra.items() if v not in (None, "")})
                cand.meta_sources.append("sidecar")
            found.append(cand)
    return found


def _manifest(folder: Path) -> dict[str, dict[str, Any]]:
    """file name -> declared metadata, from _batch.yaml or fetch.py's _index.json."""
    out: dict[str, dict[str, Any]] = {}
    index = folder / "_index.json"
    if index.exists():
        for e in json.loads(index.read_text(encoding="utf-8")):
            out[e["file"]] = {
                "title": e.get("title") or e.get("name"),
                "source": e.get("url"),
                "doc_version": e.get("doc_version"),
                "date": e.get("date") or e.get("fetched_at"),
                "superseded": e.get("superseded"),
                "family": e.get("family"),
                "pages": e.get("pages"),
            }
    batch = folder / "_batch.yaml"
    if batch.exists():
        data = yaml.safe_load(batch.read_text(encoding="utf-8")) or {}
        for e in data.get("documents") or []:
            out[e["file"]] = {k: v for k, v in e.items() if k != "file"}
    return out


def embedded_metadata(path: Path) -> dict[str, Any]:
    """Metadata the file itself carries. Only used to fill gaps in declared metadata."""
    suffix = path.suffix.lower()
    meta: dict[str, Any] = {}
    try:
        if suffix == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
            if m:
                for line in m.group(1).splitlines():
                    key, _, value = line.partition(":")
                    if value.strip():
                        meta[key.strip()] = value.strip().strip('"')
            h1 = re.search(r"^# (.+)$", text, flags=re.MULTILINE)      # the document's own title
            if h1 and "title" not in meta:
                meta["title"] = h1.group(1).strip()
        elif suffix in (".html", ".htm"):
            text = path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"<title>(.*?)</title>", text, flags=re.DOTALL | re.IGNORECASE)
            if m and m.group(1).strip():
                meta["title"] = " ".join(m.group(1).split())
            d = re.search(r'<meta\s+name="(?:date|dc\.date)"\s+content="([^"]+)"', text, re.IGNORECASE)
            if d:
                meta["date"] = d.group(1)
        elif suffix == ".pdf":
            meta.update(_pdf_info(path))
    except Exception:  # metadata is best effort; the completeness check reports gaps
        pass
    return meta


def _pdf_info(path: Path) -> dict[str, Any]:
    import pdfplumber

    meta: dict[str, Any] = {}
    with pdfplumber.open(str(path)) as pdf:
        info = pdf.metadata or {}
        meta["pages"] = len(pdf.pages)
        if str(info.get("Title", "")).strip():
            meta["title"] = str(info["Title"]).strip()
        raw = str(info.get("CreationDate") or info.get("ModDate") or "")
        m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
        if m:
            meta["date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return meta


def pdf_page_count(path: Path) -> int | None:
    """Pages the PDF says it has (its page tree), or None if it cannot be opened."""
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def to_document(cand: Candidate) -> Document:
    """A Document shell (no pages yet) with identity and metadata filled in."""
    meta = dict(embedded_metadata(cand.path))
    if meta:
        cand.meta_sources.append("embedded")
    meta.update(cand.meta)                                  # declared beats embedded
    superseded = str(meta.get("superseded", "false")).lower() == "true"
    pages = meta.get("pages")
    return Document(
        source=cand.path.name,
        doc_version=str(meta.get("doc_version") or meta.get("version") or ""),
        superseded=superseded,
        doc_id=str(meta.get("doc_id") or slug(cand.path.name)),
        title=str(meta.get("title") or ""),
        source_uri=str(meta.get("source") or meta.get("url") or ""),
        family=str(meta.get("family") or ""),
        date=str(meta.get("date") or ""),
        declared_pages=int(pages) if str(pages or "").isdigit() else None,
        sha256=sha256_file(cand.path),
        path=str(cand.path),
        batch=cand.batch,
    )


# ------------------------------------------------------------------ registry

class Registry:
    """What has been ingested into one collection. A plain JSON file on the PVC: the
    indexer Job is the only process that writes it, like the vector store itself."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.docs: dict[str, dict[str, Any]] = {}
        self.pins: dict[str, str] = {}            # family -> doc_id restored by hand
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.docs = data.get("documents", {})
            self.pins = data.get("pins", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"documents": self.docs, "pins": self.pins}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def indexed(self) -> dict[str, dict[str, Any]]:
        """Documents whose chunks are in the vector store (active or archived)."""
        return {k: v for k, v in self.docs.items() if v.get("status") in ("active", "archived")}


# ------------------------------------------------------------------ file moves

def quarantine(doc: Document, paths: LakePaths, run: str, reasons: dict[str, Any]) -> str:
    """Move a failed document out of the lake. If the source is read-only (the CI
    fixtures baked into the image), copy it instead; the report says which."""
    target_dir = paths.quarantine / run
    target_dir.mkdir(parents=True, exist_ok=True)
    src = Path(doc.path)
    target = target_dir / f"{doc.batch.replace('/', '_')}__{src.name}"
    try:
        shutil.move(str(src), target)
        how = "moved"
    except OSError:
        shutil.copy2(src, target)
        how = "copied (source is read-only)"
    (target_dir / f"{target.name}.reason.json").write_text(
        json.dumps({"doc_id": doc.doc_id, "source": doc.source, "batch": doc.batch,
                    "sha256": doc.sha256, "failed": reasons}, indent=1), encoding="utf-8")
    return f"{how} to {target}"


def archive(doc: Document, paths: LakePaths) -> str:
    """Keep a copy of a superseded version. The chunks stay in the vector store with
    superseded=true, so normal retrieval skips them and a restore is instant."""
    target_dir = paths.archive / doc.doc_id / (slug(doc.doc_version) or "unversioned")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(doc.path).name
    if not target.exists():
        shutil.copy2(doc.path, target)
    return str(target)
