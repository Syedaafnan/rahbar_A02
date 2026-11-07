#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Talking to the API, reading the golden set, writing results.json and the exit code
came with the starter kit; recall_at_k, citation_validity, summarise and gate are ours.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py).

    502/503/504 come from the Ingress, not the api: right after a rollout the
    controller can still be catching up with the new pod endpoints. Those are retried
    a few times; any other error (and the api's own answers) is final.
    """
    for attempt in range(6):
        response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
        if response.status_code not in (502, 503, 504) or attempt == 5:
            break
        print(f"  ingress returned {response.status_code}, retrying in 5 s")
        time.sleep(5)
    response.raise_for_status()
    return response.json()


def section_matches(wanted: str, retrieved_section: str) -> bool:
    """The matching rule (docs/PIPELINE.md): the golden section is contained in the
    retrieved section, ignoring case. The chunker records the full heading path
    ("4. Registration > Withdrawal from a Course"), the golden set names the heading."""
    return wanted.strip().lower() in retrieved_section.strip().lower()


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top = retrieved[:k]
    found = sum(1 for r in relevant
                if any(p["source"] == r["source"] and section_matches(r["section"], p["section"])
                       for p in top))
    return found / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = {(p["source"], p["section"]) for p in response.get("retrieved") or []}
    valid = sum(1 for c in citations if (c["source"], c["section"]) in retrieved)
    return valid / len(citations)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    per_question = []
    r3s, r5s, by_type = [], [], {}
    cited = valid = 0
    abstain_total = abstain_ok = 0
    answerable = answered = expect_total = expect_hits = 0
    for row in rows:
        resp = row["response"]
        r3 = recall_at_k(row["relevant"], resp["retrieved"], 3)
        r5 = recall_at_k(row["relevant"], resp["retrieved"], 5)
        if r5 is not None and r3 is not None:
            r3s.append(r3)
            r5s.append(r5)
            by_type.setdefault(row["type"], []).append(r5)
        n_cit = len(resp.get("citations") or [])
        cv = citation_validity(resp)
        if cv is not None:
            cited += n_cit
            valid += round(cv * n_cit)
        if row["must_abstain"]:
            abstain_total += 1
            abstain_ok += int(resp["answered_from_context"] is False)
        else:
            answerable += 1
            answered += int(bool(resp["answered_from_context"]))
        expect_hit = None
        if row.get("expect") and not row["must_abstain"]:
            expect_total += 1
            expect_hit = str(row["expect"]).lower() in resp["answer"].lower()
            expect_hits += int(expect_hit)
        per_question.append({
            "id": row["id"], "type": row["type"], "recall@3": r3, "recall@5": r5,
            "answered_from_context": resp["answered_from_context"],
            "citation_validity": cv, "expect_found": expect_hit,
            "retrieved": [f'{p["source"]} :: {p["section"]} ({p["score"]})'
                          for p in resp["retrieved"]],
            "seconds": row["seconds"],
        })
    return {
        "recall@3": _mean(r3s),
        "recall@5": _mean(r5s),
        "recall@5_by_type": {t: _mean(v) for t, v in sorted(by_type.items())},
        # No citations at all (e.g. everything abstained) cannot violate the rule.
        "citation_validity": round(valid / cited, 4) if cited else 1.0,
        "citations_checked": cited,
        "abstention_correctness": round(abstain_ok / abstain_total, 4) if abstain_total else 1.0,
        "must_abstain_questions": abstain_total,
        "answer_rate_answerable": round(answered / answerable, 4) if answerable else None,
        "expect_found_rate": round(expect_hits / expect_total, 4) if expect_total else None,
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures = []
    if baseline and baseline.get("recall@5") is not None and results["recall@5"] is not None:
        drop = baseline["recall@5"] - results["recall@5"]
        if drop > MAX_RECALL5_DROP:
            failures.append(f"recall@5 fell {drop:.3f} below the baseline "
                            f"({results['recall@5']:.3f} vs {baseline['recall@5']:.3f}); "
                            f"the limit is {MAX_RECALL5_DROP}")
    if results["abstention_correctness"] < REQUIRED_ABSTENTION:
        wrong = [q["id"] for q in results["per_question"]
                 if q["recall@5"] is None and q["answered_from_context"]]
        failures.append(f"must-abstain questions answered from context: {wrong}")
    if results["citation_validity"] < MIN_CITATION_VALIDITY:
        failures.append(f"citation validity {results['citation_validity']:.3f} is below "
                        f"{MIN_CITATION_VALIDITY}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden.name)
    results["questions"] = len(rows)
    print(f"\nrecall@3 {results['recall@3']}  recall@5 {results['recall@5']}  "
          f"by type {results['recall@5_by_type']}")
    print(f"citation validity {results['citation_validity']}  "
          f"abstention correctness {results['abstention_correctness']}")
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
