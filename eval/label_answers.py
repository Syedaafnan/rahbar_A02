#!/usr/bin/env python3
"""Hand-label answers for faithfulness (A03 B: "label at least 8 answers by hand and
report how often the judge agrees with you").

    python eval/label_answers.py eval/results/<ts>-baseline.json --who "Afnan"

For each answered question in the result file it shows the question, the exact context
the model was given and the answer, WITHOUT the judge's verdict, and asks: is every
claim in the answer supported by this context? Labels are appended to
eval/judge_labels.jsonl with the answer's hash, so the next suite run reports the
judge's agreement on exactly these answers (eval/suite.py, e2e.judge_agreement).
Both members should label; disagreements between you are worth a line in the report.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Label answers as faithful or not, by hand.")
    ap.add_argument("result", type=Path)
    ap.add_argument("--who", help="your name: written next to each label")
    ap.add_argument("--report", action="store_true",
                    help="do not label: compute the judge's agreement with the labels for this "
                         "result file and write it into the file (and its .md summary)")
    ap.add_argument("--labels", type=Path, default=HERE / "judge_labels.jsonl")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    if args.report:
        return report(args.result, result, args.labels)
    if not args.who:
        ap.error("--who is required when labelling")
    done = set()
    if args.labels.exists():
        for line in args.labels.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["id"], r["answer_sha"]))
    rows = [r for r in result["per_question"]["e2e"]
            if r["answered_from_context"] and (r["id"], r["answer_sha"]) not in done]
    print(f"{len(rows)} answered questions not labelled yet; labelling up to {args.limit}.\n"
          "Answer y (every claim is supported by the context), n (some claim is not), "
          "s (skip), q (quit).\n")
    labelled = 0
    with args.labels.open("a", encoding="utf-8") as out:
        for r in rows[:args.limit]:
            print("=" * 90)
            print(f"{r['id']}  {r.get('question', '')}\n")
            for c in r.get("context", []):
                print(f"[{c['source']} / {c['section']} p{c.get('page')}]\n{c['text']}\n")
            print(f"ANSWER: {r['answer']}\n")
            while True:
                choice = input("supported? [y/n/s/q] ").strip().lower()
                if choice in ("y", "n", "s", "q"):
                    break
            if choice == "q":
                break
            if choice == "s":
                continue
            note = input("note (optional): ").strip()
            out.write(json.dumps({"id": r["id"], "answer_sha": r["answer_sha"],
                                  "human_faithful": choice == "y", "labeller": args.who,
                                  "note": note, "result_file": args.result.name,
                                  "labelled_at": time.strftime("%Y-%m-%d")}) + "\n")
            out.flush()
            labelled += 1
    print(f"\n{labelled} labels written to {args.labels}")
    return 0


def report(path: Path, result: dict, labels_path: Path) -> int:
    """Judge vs hand labels for this result file, without re-running the suite."""
    sys.path.insert(0, str(HERE))
    from suite import judge_agreement, summary_markdown

    labels = {}
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            labels[r["id"]] = r
    rows = result["per_question"]["e2e"]
    agreement = judge_agreement(rows, labels)
    print(f"JUDGE AGREEMENT  result={path.name}  labels={labels_path.name}")
    print(f"{'id':<5} {'human':<7} {'judge':<7} agree  labeller")
    for r in rows:
        lab = labels.get(r["id"])
        if lab and lab.get("answer_sha") == r["answer_sha"] and r["faithful"] is not None:
            h, j = bool(lab["human_faithful"]), bool(r["faithful"])
            print(f"{r['id']:<5} {str(h):<7} {str(j):<7} {'yes' if h == j else 'NO':<6} "
                  f"{lab.get('labeller', '')}")
    print(json.dumps(agreement, indent=1))
    result["e2e"]["judge_agreement"] = agreement
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    path.with_suffix(".md").write_text(summary_markdown(result), encoding="utf-8")
    print(f"written into {path} and {path.with_suffix('.md').name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
