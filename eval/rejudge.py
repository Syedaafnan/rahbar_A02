#!/usr/bin/env python3
"""Re-run ONLY the faithfulness judge on the answers stored in a result file.

    python eval/rejudge.py eval/results/cluster/<ts>-pre-batch-b.json

The answers, contexts and every other metric stay exactly as they were measured; the
judge's verdicts and e2e.faithfulness are recomputed with the current judge
(eval/suite.py, Judge), and the previous value is kept as e2e.faithfulness_previous
with the judge that produced it. Used once: the first judge prompt was broken (0.08,
see the comment above JUDGE_PROMPT in suite.py), and re-generating 36 answers on a
CPU just to re-judge them would have cost an hour.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from suite import Judge, judge_agreement, load_jsonl, summary_markdown  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-judge the answers in a result file.")
    ap.add_argument("result", type=Path)
    ap.add_argument("--labels", type=Path, default=HERE / "judge_labels.jsonl")
    args = ap.parse_args()
    res = json.loads(args.result.read_text(encoding="utf-8"))
    judge = Judge()
    rows = res["per_question"]["e2e"]
    for r in rows:
        if not r["answered_from_context"]:
            continue
        r["judge_previous"] = r.get("judge")
        r["judge"] = judge(r["answer"], r.get("context") or [], r.get("question", ""))
        r["faithful"] = r["judge"]["supported"]
        print(f"  {r['id']:<4} supported={r['faithful']!s:<5} (was "
              f"{(r['judge_previous'] or {}).get('supported')})", flush=True)
    judged = [r for r in rows if r["faithful"] is not None]
    e2e = res["e2e"]
    e2e["faithfulness_previous"] = {"value": e2e.get("faithfulness"), "judge": e2e.get("judge")}
    e2e["faithfulness"] = round(sum(bool(r["faithful"]) for r in judged) / len(judged), 4) \
        if judged else None
    e2e["faithfulness_judged"] = len(judged)
    e2e["judge"] = {**judge.describe(), "rejudged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                     time.gmtime())}
    labels = {r["id"]: r for r in load_jsonl([args.labels])} if args.labels.exists() else {}
    e2e["judge_agreement"] = judge_agreement(rows, labels)
    args.result.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    args.result.with_suffix(".md").write_text(summary_markdown(res), encoding="utf-8")
    print(f"faithfulness {e2e['faithfulness_previous']['value']} -> {e2e['faithfulness']} "
          f"({len(judged)} answers); written into {args.result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
