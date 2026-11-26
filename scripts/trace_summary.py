#!/usr/bin/env python3
"""Summarise the trace log over a time window (A03 D3, paper 5.1 monitorability).

    python scripts/trace_summary.py --since 1h               # traces/ folder
    python scripts/trace_summary.py --since 24h --kubectl    # from the api pods' logs
    python scripts/trace_summary.py --from 2026-11-22T10:00 --to 2026-11-22T12:00 \
        --out eval/results/trace-summary.json

Reports: top-1 retrieval similarity distribution, abstention rate, cache hit rate,
p95 latency per stage, injection attempts flagged, per release (live / candidate).
The same signals are live on /metrics (rahbar_top1_similarity, rahbar_abstention_ratio,
rahbar_cache_hit_ratio, rahbar_stage_seconds, rahbar_injection_flags_total).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_show import iter_traces

BUCKETS = [0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.01]


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if not m:
        raise SystemExit("--since takes e.g. 30m, 2h, 7d")
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]:
                        int(m.group(1))})


def ts(t: dict[str, Any]) -> datetime:
    return datetime.strptime(t["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[max(1, math.ceil(q / 100 * len(s))) - 1], 4)


def summarise(traces: list[dict[str, Any]]) -> dict[str, Any]:
    asks = [t for t in traces if t.get("kind") == "ask"]
    top1 = [t["retrieval"]["top1_similarity"] for t in traces
            if t.get("retrieval") and t["retrieval"].get("top1_similarity") is not None]
    hist = {f"{BUCKETS[i]:.2f}-{min(BUCKETS[i + 1], 1.0):.2f}":
            sum(BUCKETS[i] <= v < BUCKETS[i + 1] for v in top1) for i in range(len(BUCKETS) - 1)}
    answered = [t for t in asks if t.get("response")]
    abstained = [t for t in answered if not t["response"]["answered_from_context"]]
    cache = [t["cache"]["status"] for t in asks]
    lookups = [c for c in cache if c in ("hit", "miss")]
    stages = sorted({s for t in traces for s in t.get("latency_ms", {})})
    injections = [a for t in traces for a in t.get("guardrail") or []]
    reasons: dict[str, int] = {}
    for t in answered:
        reasons[t["response"]["reason"]] = reasons.get(t["response"]["reason"], 0) + 1
    return {
        "traces": len(traces), "asks": len(asks), "retrieves": len(traces) - len(asks),
        "top1_similarity": {"n": len(top1), "min": min(top1) if top1 else None,
                            "p25": pct(top1, 25), "p50": pct(top1, 50), "p75": pct(top1, 75),
                            "max": max(top1) if top1 else None, "histogram": hist},
        "abstention_rate": round(len(abstained) / len(answered), 4) if answered else None,
        "outcomes": reasons,
        "cache_hit_rate": round(cache.count("hit") / len(lookups), 4) if lookups else None,
        "cache": {s: cache.count(s) for s in ("hit", "miss", "skip")},
        "latency_p95_ms": {s: pct([t["latency_ms"][s] for t in traces if s in t["latency_ms"]], 95)
                           for s in stages},
        "injection_flags": {"total": len(injections),
                            "passages_dropped": sum(a["target"] == "passage" for a in injections),
                            "questions_flagged": sum(a["target"] == "question" for a in injections),
                            "by_pattern": _count(a["pattern"] for a in injections),
                            "documents": sorted({a.get("doc_id") or a.get("source", "")
                                                 for a in injections if a["target"] == "passage"})},
        "collections": _count(f"{t['collection']['name']} / {t['embedding']['model']}" for t in traces),
    }


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarise traces over a time window.")
    ap.add_argument("--since", help="window ending now, e.g. 1h, 30m, 7d")
    ap.add_argument("--from", dest="start", help="UTC start, e.g. 2026-11-22T10:00")
    ap.add_argument("--to", dest="end", help="UTC end")
    ap.add_argument("--dir", type=Path)
    ap.add_argument("--kubectl", action="store_true")
    ap.add_argument("-n", "--namespace", default="rahbar")
    ap.add_argument("-l", "--selector", default="app.kubernetes.io/part-of=rahbar,app.kubernetes.io/component=query")
    ap.add_argument("--out", type=Path, help="also write the summary as JSON")
    args = ap.parse_args()
    now = datetime.now(UTC)
    start = now - parse_since(args.since) if args.since else \
        (datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else datetime.min.replace(tzinfo=UTC))
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else now
    traces = [t for t in iter_traces(args.dir, args.kubectl, args.namespace, args.selector)
              if start <= ts(t) <= end]
    releases = sorted({t.get("release", "live") for t in traces})
    result = {"window": {"from": start.isoformat() if start.year > 1 else None, "to": end.isoformat()},
              "all": summarise(traces),
              "by_release": {r: summarise([t for t in traces if t.get("release", "live") == r])
                             for r in releases}}
    a = result["all"]
    print(f"TRACE SUMMARY  window {result['window']['from'] or 'beginning'} .. {result['window']['to']}")
    print(f"traces {a['traces']} (ask {a['asks']}, retrieve {a['retrieves']})  "
          f"collections {a['collections']}")
    s = a["top1_similarity"]
    print(f"top-1 similarity: n={s['n']} min={s['min']} p25={s['p25']} p50={s['p50']} "
          f"p75={s['p75']} max={s['max']}")
    peak = max(s["histogram"].values() or [1]) or 1
    for bucket, n in s["histogram"].items():
        print(f"   {bucket}  {'#' * round(40 * n / peak):<40} {n}")
    print(f"abstention rate: {a['abstention_rate']}   outcomes {a['outcomes']}")
    print(f"cache hit rate:  {a['cache_hit_rate']}   {a['cache']}")
    print("p95 latency per stage (ms): " + ", ".join(f"{k} {v}" for k, v in a["latency_p95_ms"].items()))
    inj = a["injection_flags"]
    print(f"injection attempts flagged: {inj['total']} (passages dropped {inj['passages_dropped']}, "
          f"questions flagged {inj['questions_flagged']}) {inj['by_pattern']} docs {inj['documents']}")
    for r, sub in result["by_release"].items():
        print(f"  release {r}: traces {sub['traces']}, abstention {sub['abstention_rate']}, "
              f"p95 total {sub['latency_p95_ms'].get('total')} ms")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
