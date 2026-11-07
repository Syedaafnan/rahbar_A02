"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import IndexerSettings, get_settings
from .models import Chunk, Document


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def load_documents(settings: IndexerSettings) -> list[Document]:
    """Stages 1-2 for every document: extract (with OCR), then clean."""
    from .clean import clean
    from .extract import extract

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    return [clean(extract(str(path), version, old)) for path, version, old in documents]


def build_chunks(settings: IndexerSettings, docs: list[Document] | None = None) -> list[Chunk]:
    """Stage 3 for every document: chunk (after stages 1-2, unless docs are given)."""
    from .chunk import chunk, chunk_fixed

    splitter = chunk_fixed if settings.chunk_strategy == "fixed" else chunk
    all_chunks: list[Chunk] = []
    original_words = 0
    for doc in docs if docs is not None else load_documents(settings):
        words = sum(len(p.text.split()) for p in doc.pages)
        original_words += words
        pieces = splitter(doc, settings.chunk_size, settings.chunk_overlap)
        log("chunked", source=doc.source, pages=len(doc.pages), words=words,
            ocr_pages=[p.page for p in doc.pages if p.ocr], chunks=len(pieces),
            doc_version=doc.doc_version, superseded=doc.superseded)
        all_chunks.extend(pieces)
    stored = sum(len(c.text.split()) for c in all_chunks)
    # Storage amplification (section 3.1): stored words / original words.
    log("chunking summary", strategy=settings.chunk_strategy, size=settings.chunk_size,
        overlap=settings.chunk_overlap, chunks=len(all_chunks), original_words=original_words,
        stored_words=stored, amplification=round(stored / max(1, original_words), 3))
    return all_chunks


def run_pipeline(settings: IndexerSettings, dump: str | None = None) -> int:
    from .embed import embed
    from .store import store

    all_chunks = build_chunks(settings)
    if dump:                                  # inspect chunks without a model or Chroma
        Path(dump).write_text(json.dumps([c.__dict__ for c in all_chunks], indent=1),
                              encoding="utf-8")
        log("chunks written", file=dump)
        return len(all_chunks)
    vectors = embed([c.text for c in all_chunks], settings)
    log("embedded", chunks=len(vectors), dimension=len(vectors[0]), model=settings.embed_model)
    return store(all_chunks, vectors, settings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--dump", metavar="FILE",
                        help="extract, clean and chunk, write the chunks to FILE, then stop")
    parser.add_argument("--dump-docs", metavar="FILE",
                        help="extract and clean only, write the documents to FILE, then stop "
                             "(input for eval/chunk_experiment.py)")
    args = parser.parse_args()
    if args.dump_docs:
        docs = load_documents(get_settings())
        Path(args.dump_docs).write_text(json.dumps(
            [{**d.__dict__, "pages": [p.__dict__ for p in d.pages]} for d in docs]),
            encoding="utf-8")
        log("documents written", file=args.dump_docs, documents=len(docs))
        return 0
    settings = get_settings()
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        else:
            count = run_pipeline(settings, dump=args.dump)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
