#!/usr/bin/env python3
"""Release gate (A03 C2, paper 4.2.6): compare a candidate's result file with the
baseline's, against thresholds from a config file, and exit 0 (release) or 1 (reject).

    python eval/gate.py --candidate eval/results/<ts>-candidate.json \
                        --baseline  eval/results/<ts>-baseline.json \
                        --config    eval/gate.yaml

On the cluster the thresholds come from the ConfigMap `rahbar-gate` mounted at
/config/gate.yaml, so editing a threshold changes the outcome with no image rebuild
(k8s/gate-job.yaml re-applies the gate to stored result files in seconds).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def load_thresholds(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("gate", data)


def _get(results: dict[str, Any], dotted: str) -> Any:
    cur: Any = results
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate(candidate: dict[str, Any], baseline: dict[str, Any] | None,
             t: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per rule: {rule, value, limit, pass, detail}."""
    rows: list[dict[str, Any]] = []

    def add(rule: str, value: Any, limit: str, ok: bool | None, detail: str = "") -> None:
        rows.append({"rule": rule, "value": value, "limit": limit, "pass": ok, "detail": detail})

    r5 = _get(candidate, "component.clean.recall@5")
    b5 = _get(baseline, "component.clean.recall@5") if baseline else None
    drop = t.get("recall_at_5_max_drop")
    if drop is not None:
        if r5 is None or b5 is None:
            add("recall@5 drop vs baseline", r5, f"<= {drop}", None, "no baseline")
        else:
            add("recall@5 drop vs baseline", round(b5 - r5, 4), f"<= {drop}",
                b5 - r5 <= drop + 1e-9, f"candidate {r5} vs baseline {b5}")

    for key, path, op in [
        ("faithfulness_min", "e2e.faithfulness", ">="),
        ("citation_validity_min", "e2e.citation_validity", ">="),
        ("abstention_correctness_min", "e2e.abstention_correctness", ">="),
        ("embedding_gap_min", "module.gap", ">="),
        ("p95_latency_ms_max", "e2e.latency_ms.p95", "<="),
    ]:
        if t.get(key) is None:
            continue
        value = _get(candidate, path)
        limit = t[key]
        ok = None if value is None else (value >= limit if op == ">=" else value <= limit)
        add(path, value, f"{op} {limit}", ok, "" if value is not None else "metric missing")

    if t.get("abstention_not_below_baseline"):
        a = _get(candidate, "e2e.abstention_correctness")
        b = _get(baseline, "e2e.abstention_correctness") if baseline else None
        if a is None or b is None:
            add("abstention vs baseline", a, ">= baseline", None, "no baseline")
        else:
            add("abstention vs baseline", a, f">= {b}", a >= b - 1e-9)
    return rows


def decide(rows: list[dict[str, Any]]) -> bool:
    """Release only if no rule failed. A rule that could not be evaluated (None) does
    not fail the gate, but it is printed, so a missing baseline is visible."""
    return not any(r["pass"] is False for r in rows)


def table(rows: list[dict[str, Any]], released: bool, label: str = "") -> str:
    mark = {True: "PASS", False: "FAIL", None: "skip"}
    lines = [f"RELEASE GATE {label}".rstrip(),
             f"{'rule':<34} {'value':>12}  {'limit':<14} result  detail"]
    for r in rows:
        value = "-" if r["value"] is None else r["value"]
        lines.append(f"{r['rule']:<34} {value!s:>12}  {r['limit']:<14} {mark[r['pass']]:<7} "
                     f"{r['detail']}")
    lines.append(f"\nDECISION: {'RELEASE (gate passed)' if released else 'REJECT (gate failed)'}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply the release gate to a result file.")
    ap.add_argument("--candidate", type=Path, required=True)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("gate.yaml"))
    ap.add_argument("--out", type=Path, help="write the gate decision as JSON here")
    args = ap.parse_args()
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) \
        if args.baseline and args.baseline.exists() else None
    thresholds = load_thresholds(args.config)
    rows = evaluate(candidate, baseline, thresholds)
    released = decide(rows)
    print(f"candidate: {args.candidate}\nbaseline:  {args.baseline if baseline else '(none)'}\n"
          f"config:    {args.config}  {json.dumps(thresholds)}\n")
    print(table(rows, released, candidate.get("label", "")))
    if args.out:
        args.out.write_text(json.dumps({"candidate": str(args.candidate),
                                        "baseline": str(args.baseline) if baseline else None,
                                        "thresholds": thresholds, "rules": rows,
                                        "released": released}, indent=1), encoding="utf-8")
    return 0 if released else 1


if __name__ == "__main__":
    sys.exit(main())
