#!/usr/bin/env python3
"""Send user-style traffic to the live api (cache ON), for D3/D4: the first questions
from eval/live_queries.jsonl, some asked twice so the semantic cache is exercised, plus
the D4 probe that retrieves Batch B's injected chunk. Prints trace id, cache status,
outcome and any guardrail action per question.

    python scripts/live_traffic.py [--n 6]
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D4 = "Can any exam be rescheduled at any time without documents, and is there a fee?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    a = ap.parse_args()
    live = [json.loads(line) for line in (ROOT / "eval" / "live_queries.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    qs = [D4] + [q["query"] for q in live[:a.n]] + [q["query"] for q in live[:3]]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pf = subprocess.Popen(["kubectl", "-n", "rahbar", "port-forward", "svc/api", f"{port}:8000"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    try:
        for q in qs:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ask",
                                         data=json.dumps({"question": q}).encode(),
                                         headers={"Content-Type": "application/json"})
            t = time.time()
            with urllib.request.urlopen(req, timeout=600) as r:
                body, cache = json.loads(r.read()), r.headers.get("X-Cache")
            print(f"{body['trace_id']}  cache={cache:<4} {time.time() - t:6.1f}s  "
                  f"answered={body['answered_from_context']!s:<5}  {q[:60]}", flush=True)
    finally:
        pf.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
