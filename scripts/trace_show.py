#!/usr/bin/env python3
"""Trace tool (A03 D2): given a trace id, print the exact context and prompt that
produced that answer, and check the prompt against the hash recorded at answer time.

    python scripts/trace_show.py <trace_id>                    # searches traces/
    python scripts/trace_show.py <trace_id> --dir /traces      # the PVC, inside a pod
    python scripts/trace_show.py <trace_id> --kubectl          # kubectl logs of the api pods
    python scripts/trace_show.py --last                        # the newest trace

A trace id is the X-Trace-ID response header (= request_id), also in every eval result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def iter_traces(directory: Path | None, kubectl: bool, namespace: str,
                selector: str) -> Iterator[dict[str, Any]]:
    if kubectl:
        out = subprocess.run(["kubectl", "logs", "-n", namespace, "-l", selector,
                              "--tail=-1", "--prefix=false", "--max-log-requests=20"],
                             capture_output=True, text=True, encoding="utf-8", check=False).stdout
        lines: Iterator[str] = iter(out.splitlines())
        yield from _parse(lines)
        return
    for f in sorted((directory or ROOT / "traces").rglob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            yield from _parse(fh)


def _parse(lines: Any) -> Iterator[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if line.startswith('{"type": "trace"'):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def prompt_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def show(t: dict[str, Any]) -> None:
    bar = "=" * 100
    print(bar)
    print(f"TRACE {t['trace_id']}  {t['timestamp']}  kind={t.get('kind')}  release={t.get('release')}"
          f"  pod={t.get('pod')}")
    print(bar)
    q = t["query"]
    print(f"user query:      {q['user_query']}")
    print(f"rewritten query: {q['rewritten_query']}   ({q.get('rewriter')})")
    e, c = t["embedding"], t["collection"]
    print(f"embedding:       {e['model']}  version {e.get('version')}  prefix {e.get('query_prefix')!r}")
    print(f"collection:      {c['name']}  index_version {c.get('index_version')}  "
          f"built with {c.get('built_with_model')}  chunk {c.get('chunk_size')}/{c.get('chunk_overlap')}")
    print(f"cache:           {t['cache']}")
    r = t.get("retrieval") or {}
    if r:
        print(f"\nRETRIEVAL  top_k={r.get('top_k')}  filters={r.get('filters')}  "
              f"floor={r.get('relevance_floor')}  reranker={r.get('reranker')}")
        print(f"  {'#':<3}{'score':>7}  {'version':<9} {'sup':<4} {'chunk_id':<52} section")
        for x in r.get("results", []):
            mark = "*" if x["chunk_id"] in r.get("above_floor", []) else " "
            print(f"  {x['rank']:<3}{x['score']:>7.4f}{mark} {str(x.get('doc_version'))[:9]:<9} "
                  f"{'yes' if x.get('superseded') else 'no':<4} {x['chunk_id'][:52]:<52} "
                  f"{x['source']} :: {x['section'][:60]} (p{x.get('page')})")
        print("  (* = cleared the relevance floor)")
    if t.get("guardrail"):
        print("\nGUARDRAIL")
        for a in t["guardrail"]:
            print(f"  {a['action'].upper():<8} {a['target']:<9} pattern={a['pattern']:<20} "
                  f"{a.get('chunk_id', '')}  matched: {a['matched']!r}")
    p = t.get("prompt")
    if p:
        ok = prompt_hash(p["messages"]) == p["sha256"]
        print(f"\nPROMPT  sha256 {p['sha256']}  ({'matches the recorded hash' if ok else 'HASH MISMATCH'})")
        print(f"passages in the prompt: {p['passages']}")
        for m in p["messages"]:
            print(f"\n----- {m['role']} -----\n{m['content']}")
    else:
        print("\nPROMPT  none: the model was not called (abstained before generation, or cache hit)")
    g = t.get("generation")
    if g:
        print(f"\nGENERATION  {json.dumps(g)}")
    resp = t.get("response")
    if resp:
        print(f"\nRESPONSE  reason={resp['reason']}  answered_from_context={resp['answered_from_context']}"
              f"  confidence={resp['confidence']}")
        print(f"  {resp['answer']}")
        for cit in resp["citations"]:
            print(f"  cites: {cit['source']} :: {cit['section']} (p{cit.get('page')})")
    print(f"\nLATENCY ms  {json.dumps(t['latency_ms'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Print the context and prompt behind an answer.")
    ap.add_argument("trace_id", nargs="?")
    ap.add_argument("--dir", type=Path, help="folder with traces-*.jsonl (default: traces/)")
    ap.add_argument("--kubectl", action="store_true", help="read kubectl logs instead")
    ap.add_argument("-n", "--namespace", default="rahbar")
    ap.add_argument("-l", "--selector", default="app.kubernetes.io/part-of=rahbar,app.kubernetes.io/component=query")
    ap.add_argument("--last", action="store_true", help="show the newest trace")
    ap.add_argument("--json", action="store_true", help="print the raw record")
    args = ap.parse_args()
    if not args.trace_id and not args.last:
        ap.error("give a trace id or --last")
    found = None
    for t in iter_traces(args.dir, args.kubectl, args.namespace, args.selector):
        if args.last:
            if found is None or t["timestamp"] > found["timestamp"]:
                found = t
        elif t["trace_id"] == args.trace_id or t["trace_id"].startswith(args.trace_id):
            found = t
            break
    if found is None:
        print(f"trace {args.trace_id or '(last)'} not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found, indent=1, ensure_ascii=False))
    else:
        show(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
