"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run                  # ingest: discover, extract, clean, VERIFY,
                                            # chunk, embed only what is new, store
    python -m pipeline.run --count-by-doc   # chunks per document id (orphan check, A2)
    python -m pipeline.run --restore DOC_ID # roll a family back to an archived version
    python -m pipeline.run --unpin FAMILY   # undo a restore: newest version wins again
    python -m pipeline.run --dummy          # week 1: three fake chunks, no model needed
    python -m pipeline.run --dump FILE      # extract, clean and chunk, then stop

The A03 data management lifecycle (paper Figure 3, "ingest -> verify -> update data
lake -> update retrieval sources") is `ingest()` below. Exit code 0 means the index
was written; anything else fails the Job, and Kubernetes retries it up to the Job's
backoffLimit. This process, and only this process, writes to the vector store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import IndexerSettings, get_settings
from .models import Chunk, Document, Page

EXTRACT_CACHE_VERSION = "1"      # bump when extract.py or clean.py change their output


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}, default=str), flush=True)


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """(path, doc_version, superseded) for every document: the A02 view of the lake,
    used by tests/test_starter_dummy.py."""
    from .lake import discover as lake_discover
    from .lake import to_document

    return [(Path(d.path), d.doc_version or "unknown", d.superseded)
            for d in (to_document(c) for c in lake_discover(corpus_dir))]


# ------------------------------------------------------------------ extraction

def extract_and_clean(doc: Document, cache_dir: Path | None = None) -> Document:
    """Stages 1-2 for one document, cached by content hash: an unchanged file is never
    extracted (or OCR'd) twice. A failure is recorded, not raised: the verification
    stage turns it into a quality failure and quarantines the file."""
    from .clean import clean
    from .extract import extract

    cache = cache_dir / f"{doc.sha256}-v{EXTRACT_CACHE_VERSION}.json" if cache_dir else None
    if cache is not None and cache.exists():
        doc.pages = [Page(**p) for p in json.loads(cache.read_text(encoding="utf-8"))]
        return doc
    try:
        extracted = extract(doc.path, doc.doc_version, doc.superseded)
        doc.pages = clean(extracted).pages
    except Exception as exc:  # corrupt PDF, unreadable file, OCR crash
        doc.extract_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        doc.pages = []
        return doc
    if cache is not None:
        cache.write_text(json.dumps([asdict(p) for p in doc.pages]), encoding="utf-8")
    return doc


def load_documents(settings: IndexerSettings) -> list[Document]:
    """Stages 1-2 for every document in the corpus folder (no verification)."""
    from .lake import discover as lake_discover
    from .lake import to_document

    candidates = lake_discover(settings.corpus_dir)
    if not candidates:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    return [extract_and_clean(to_document(c)) for c in candidates]


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
        log("chunked", source=doc.source, doc_id=doc.doc_id, pages=len(doc.pages), words=words,
            ocr_pages=[p.page for p in doc.pages if p.ocr], chunks=len(pieces),
            doc_version=doc.doc_version, superseded=doc.superseded)
        all_chunks.extend(pieces)
    stored = sum(len(c.text.split()) for c in all_chunks)
    # Storage amplification (section 3.1): stored words / original words.
    log("chunking summary", strategy=settings.chunk_strategy, size=settings.chunk_size,
        overlap=settings.chunk_overlap, chunks=len(all_chunks), original_words=original_words,
        stored_words=stored, amplification=round(stored / max(1, original_words), 3))
    return all_chunks


# ------------------------------------------------------------------ ingestion

def ingest(settings: IndexerSettings, client: Any = None,
           embed_fn: Any = None) -> dict[str, Any]:
    """The data management lifecycle: ingest -> verify -> update the data lake ->
    update the retrieval sources. Returns the ingestion summary (also written to
    reports/ingest-<run>.json on the PVC)."""
    from . import lake
    from .embed import embed
    from .store import sync
    from .verify import summary_table, verify

    run = lake.now_stamp()
    paths = lake.LakePaths(Path(settings.data_dir)) if settings.data_dir else None
    if paths:
        paths.ensure()
        n = 1
        while (paths.reports / f"verify-{run}.json").exists():   # two runs in one second
            n += 1
            run = f"{lake.now_stamp()}-{n}"
    registry = lake.Registry(paths.state(settings.collection) / "registry.json") if paths \
        else lake.Registry(Path(os.devnull + ".registry"))
    embed_fn = embed_fn or (lambda texts: embed(texts, settings))

    # 1. ingest: everything in the lake, with its declared metadata
    candidates = lake.discover(settings.corpus_dir)
    if not candidates:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    docs = [extract_and_clean(lake.to_document(c), paths.extract_cache if paths else None)
            for c in candidates]
    log("discovered", documents=len(docs), batches=sorted({d.batch for d in docs}))

    # 2. verify: four checks per document
    def embed_docs(batch: list[Document]) -> dict[str, list[float]]:
        return _doc_vectors(batch, settings, paths, embed_fn)

    verdicts = verify(docs, embed_docs, pins=registry.pins, threshold=settings.near_dup_threshold)
    report = {"run": run, "collection": settings.collection, "embed_model": settings.embed_model,
              "near_dup_threshold": settings.near_dup_threshold,
              "documents": [v.row() for v in verdicts]}
    print("\nVERIFICATION REPORT  run=" + run + "\n" + summary_table(verdicts) + "\n", flush=True)

    # 3. update the data lake: quarantine failures, archive superseded versions
    for v in verdicts:
        failed = {c: r["reason"] for c, r in v.checks.items() if r["pass"] is False}
        if settings.lake_read_only and v.decision in ("quarantine", "archive"):
            v.checks[v.decision] = {"pass": None, "reason": "LAKE_READ_ONLY: file left in place"}
        elif v.decision == "quarantine" and paths:
            v.checks["quarantine"] = {"pass": False, "reason": lake.quarantine(v.doc, paths, run, failed)}
        elif v.decision == "archive" and paths:
            v.checks["archive"] = {"pass": True, "reason": f"copied to {lake.archive(v.doc, paths)}"}
    report["documents"] = [{**v.row(), "lake": v.checks.get("quarantine") or v.checks.get("archive")}
                           for v in verdicts]

    # 4. update the retrieval sources: only what changed is embedded
    indexable = [v.doc for v in verdicts if v.decision in ("index", "archive")]
    chunks = build_chunks(settings, indexable)
    # A changed document that now fails verification keeps serving its last good
    # version: its previous chunk ids are kept instead of deleted.
    seen = {c.id for c in chunks}
    keep_ids: list[str] = []
    for v in verdicts:
        prev = registry.docs.get(v.doc.doc_id)
        if v.decision == "quarantine" and prev and prev.get("status") in ("active", "archived"):
            keep_ids.extend(i for i in prev.get("chunk_ids", []) if i not in seen)
    counts = sync(chunks, embed_fn, settings, client=client, keep_ids=set(keep_ids))

    # 5. remember what is where (the registry)
    seen_docs = set()
    by_doc: dict[str, list[str]] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c.id)
    for v in verdicts:
        d = v.doc
        seen_docs.add(d.doc_id)
        prev = registry.docs.get(d.doc_id, {})
        status = {"index": "active", "archive": "archived"}.get(v.decision, "quarantined")
        if status == "quarantined" and prev.get("status") in ("active", "archived") \
                and prev.get("sha256") != d.sha256:
            status = prev["status"]            # still serving the last good version
            entry = {**prev, "last_rejected": {"sha256": d.sha256, "run": run}}
        else:
            entry = {"doc_id": d.doc_id, "source": d.source, "title": d.title, "family": d.family,
                     "doc_version": d.doc_version, "batch": d.batch, "sha256": d.sha256,
                     "path": d.path, "status": status, "chunk_ids": by_doc.get(d.doc_id, []),
                     "first_seen": prev.get("first_seen", run), "last_seen": run}
        registry.docs[d.doc_id] = entry
    deleted = []
    for doc_id, entry in registry.docs.items():
        if doc_id not in seen_docs and entry.get("status") in ("active", "archived"):
            entry.update(status="deleted", deleted_in=run, chunk_ids=[])
            deleted.append(doc_id)
    if paths:
        registry.save()

    decisions = [v.decision for v in verdicts]
    summary = {
        "run": run, "collection": settings.collection, "embed_model": settings.embed_model,
        "documents_seen": len(verdicts), "indexed_active": decisions.count("index"),
        "archived": decisions.count("archive"), "quarantined": decisions.count("quarantine"),
        "deleted_documents": deleted, **counts,
    }
    report["ingest"] = summary
    if paths:
        (paths.reports / f"verify-{run}.json").write_text(json.dumps(report, indent=1),
                                                         encoding="utf-8")
        (paths.reports / f"ingest-{run}.json").write_text(json.dumps(summary, indent=1),
                                                         encoding="utf-8")
    print(_summary_text(summary), flush=True)
    log("ingest summary", **summary)
    return summary


def _summary_text(s: dict[str, Any]) -> str:
    return (f"\nINGEST SUMMARY  run={s['run']}  collection={s['collection']}  "
            f"model={s['embed_model']}\n"
            f"  documents: seen {s['documents_seen']}, indexed (active) {s['indexed_active']}, "
            f"archived {s['archived']}, quarantined {s['quarantined']}, "
            f"deleted {len(s['deleted_documents'])} {s['deleted_documents'] or ''}\n"
            f"  chunks:    wanted {s['chunks_wanted']}, EMBEDDED {s['embedded']}, "
            f"skipped (unchanged) {s['skipped_unchanged']}, metadata updated "
            f"{s['metadata_updated']}, deleted {s['deleted']}, kept (last good) {s['kept']}\n"
            f"  embedded by document: {s['embedded_by_doc'] or '{}'}\n"
            f"  deleted by document:  {s['deleted_by_doc'] or '{}'}\n"
            f"  collection now holds {s['total']} chunks, index_version {s['index_version']}\n")


def _doc_vectors(docs: list[Document], settings: IndexerSettings, paths: Any,
                 embed_fn: Any) -> dict[str, list[float]]:
    """Whole-document vectors for the near-duplicate check (mean of evenly spaced
    windows), cached by content hash and model, so an unchanged corpus calls the model
    for nothing."""
    from .verify import mean_vector, near_dup_windows

    out: dict[str, list[float]] = {}
    cache_dir = paths.root / "state" / "doc-vectors" / settings.embed_model.replace(":", "_")         if paths else None
    for d in docs:
        f = cache_dir / f"{d.sha256}.json" if cache_dir else None
        if f is not None and f.exists():
            out[d.doc_id] = json.loads(f.read_text(encoding="utf-8"))
            continue
        out[d.doc_id] = mean_vector(embed_fn(near_dup_windows(d)))
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(out[d.doc_id]), encoding="utf-8")
    return out


# ------------------------------------------------------------------ restore / count

def restore(settings: IndexerSettings, doc_id: str, client: Any = None) -> dict[str, Any]:
    """Rollback (A2): make an archived version the active one again. Its family is
    pinned to it, so the next scheduled ingest keeps it until `--unpin FAMILY`."""
    from . import lake
    from .store import set_status

    if not settings.data_dir:
        raise SystemExit("--restore needs DATA_DIR (the data lake PVC)")
    paths = lake.LakePaths(Path(settings.data_dir))
    registry = lake.Registry(paths.state(settings.collection) / "registry.json")
    target = registry.docs.get(doc_id)
    if not target or target.get("status") not in ("active", "archived"):
        raise SystemExit(f"{doc_id} is not in the index (status: "
                         f"{(target or {}).get('status', 'unknown')}); nothing to restore")
    family = target["family"]
    flips = {doc_id: False}
    for other_id, other in registry.indexed().items():
        if other_id != doc_id and other.get("family") == family:
            flips[other_id] = True
    relabelled = set_status(settings, flips, client=client)
    for d, superseded in flips.items():
        registry.docs[d]["status"] = "archived" if superseded else "active"
    registry.pins[family] = doc_id
    registry.save()
    result = {"restored": doc_id, "family": family,
              "archived": [d for d, s in flips.items() if s], "chunks_relabelled": relabelled}
    log("restored", **result)
    return result


def unpin(settings: IndexerSettings, family: str) -> None:
    from . import lake

    paths = lake.LakePaths(Path(settings.data_dir))
    registry = lake.Registry(paths.state(settings.collection) / "registry.json")
    registry.pins.pop(family, None)
    registry.save()
    log("unpinned; the next ingest makes the newest version active again", family=family)


def print_count_by_doc(settings: IndexerSettings, client: Any = None) -> int:
    """Chunks in the vector store per doc_id, next to the registry's view. An orphan is
    a doc_id with chunks in the store that the registry says is deleted, quarantined
    or unknown."""
    from . import lake
    from .store import count_by_doc

    counts = count_by_doc(settings, client=client)
    registry = lake.Registry(lake.LakePaths(Path(settings.data_dir)).state(settings.collection)
                             / "registry.json") if settings.data_dir else None
    docs = dict(registry.docs) if registry else {}
    print(f"\nCHUNKS BY DOCUMENT  collection={settings.collection}")
    print(f"{'doc_id':<58} {'registry':<12} {'chunks':>6}  store status / version")
    orphans = 0
    for doc_id in sorted(set(counts) | set(docs)):
        reg = docs.get(doc_id, {}).get("status", "unknown")
        row = counts.get(doc_id, {"chunks": 0, "status": "-", "doc_version": "-"})
        orphan = row["chunks"] > 0 and reg not in ("active", "archived")
        orphans += int(orphan)
        print(f"{doc_id[:58]:<58} {reg:<12} {row['chunks']:>6}  {row['status']} / "
              f"{row['doc_version']}{'   <-- ORPHAN' if orphan else ''}")
    total = sum(r["chunks"] for r in counts.values())
    print(f"\n{total} chunks, {len(counts)} documents in the store, {orphans} orphaned documents")
    return 1 if orphans else 0


def wait_for_dependencies(settings: IndexerSettings, need_ollama: bool = True,
                          timeout: float | None = None, client: Any = None) -> list[str]:
    """Wait until Chroma (and Ollama) answer, then return the names still down ([] = ok).

    The Job is applied together with the Chroma StatefulSet, so on a fresh cluster it
    can start while Chroma is still pulling its image. Without this wait the first
    connection fails, the Job burns its three attempts in ~30 s and is marked Failed.
    """
    import httpx

    timeout = float(os.environ.get("INDEXER_WAIT_SECONDS", 180)) if timeout is None else timeout
    urls = {"chroma": f"http://{settings.chroma_host}:{settings.chroma_port}/api/v2/heartbeat"}
    if need_ollama:
        urls["ollama"] = f"{settings.ollama_url.rstrip('/')}/api/tags"
    http = client or httpx.Client(timeout=3.0)
    deadline = time.monotonic() + timeout
    while True:
        down = []
        for name, url in urls.items():
            try:
                if http.get(url).status_code != 200:
                    down.append(name)
            except Exception:
                down.append(name)
        if not down or time.monotonic() >= deadline:
            return down
        log("waiting for dependencies", down=down)
        time.sleep(3)


def run_pipeline(settings: IndexerSettings, dump: str | None = None) -> int:
    if dump:                                  # inspect chunks without a model or Chroma
        all_chunks = build_chunks(settings)
        Path(dump).write_text(json.dumps([c.__dict__ for c in all_chunks], indent=1),
                              encoding="utf-8")
        log("chunks written", file=dump)
        return len(all_chunks)
    return int(ingest(settings)["total"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--dump", metavar="FILE",
                        help="extract, clean and chunk, write the chunks to FILE, then stop")
    parser.add_argument("--dump-docs", metavar="FILE",
                        help="extract and clean only, write the documents to FILE, then stop "
                             "(input for eval/chunk_experiment.py)")
    parser.add_argument("--count-by-doc", action="store_true",
                        help="print chunks per document id and flag orphans")
    parser.add_argument("--restore", metavar="DOC_ID",
                        help="make an archived version active again (rollback)")
    parser.add_argument("--unpin", metavar="FAMILY", help="undo a restore for a family")
    args = parser.parse_args()
    if args.dump_docs:
        docs = load_documents(get_settings())
        Path(args.dump_docs).write_text(json.dumps(
            [{"source": d.source, "doc_version": d.doc_version, "superseded": d.superseded,
              "pages": [p.__dict__ for p in d.pages]} for d in docs]), encoding="utf-8")
        log("documents written", file=args.dump_docs, documents=len(docs))
        return 0
    settings = get_settings()
    if args.unpin:
        unpin(settings, args.unpin)
        return 0
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        data_dir=settings.data_dir or "-", corpus_dir=settings.corpus_dir,
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    if not args.dump:                         # --dump needs neither Chroma nor a model
        need_model = not (args.dummy or args.count_by_doc or args.restore)
        down = wait_for_dependencies(settings, need_ollama=need_model)
        if down:
            log("dependencies not reachable, giving up", down=down)
            return 1
    try:
        if args.count_by_doc:
            return print_count_by_doc(settings)
        if args.restore:
            restore(settings, args.restore)
            return print_count_by_doc(settings)
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
