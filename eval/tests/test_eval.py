"""Unit tests for the A03 evaluation metrics, the release gate and coverage.
Run with:  cd eval && python -m pytest -q"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from coverage_check import group_topics, nearest  # noqa: E402
from gate import decide, evaluate  # noqa: E402
from ragops_metrics import (  # noqa: E402
    add_typos,
    citation_correct,
    citation_validity,
    diff_metrics,
    ndcg_at_k,
    overlap_support,
    percentile,
    recall_at_k,
    reciprocal_rank,
)

GOLD = [{"source": "h.pdf", "section": "Withdrawal from a Course", "page": 22},
        {"source": "g.html", "section": "Warning/Probation", "page": 1}]


def p(source: str, section: str, page: int = 1) -> dict:
    return {"source": source, "section": section, "page": page, "score": 0.8}


def test_recall_mrr_ndcg_on_a_known_ranking() -> None:
    got = [p("x.pdf", "Other"), p("h.pdf", "4. Registration > Withdrawal from a Course"),
           p("y.pdf", "Other"), p("z.pdf", "Other"), p("g.html", "Warning/Probation")]
    assert recall_at_k(GOLD, got, 3) == 0.5
    assert recall_at_k(GOLD, got, 5) == 1.0
    assert reciprocal_rank(GOLD, got) == 0.5
    expected = (1 / math.log2(3) + 1 / math.log2(6)) / (1 + 1 / math.log2(3))
    assert ndcg_at_k(GOLD, got, 5) == pytest.approx(expected)
    assert recall_at_k([], got, 5) is None


def test_same_relevant_item_is_counted_once_in_ndcg() -> None:
    got = [p("h.pdf", "Withdrawal from a Course"), p("h.pdf", "Withdrawal from a Course")]
    assert ndcg_at_k(GOLD[:1], got, 5) == 1.0


def test_typos_are_deterministic_and_change_the_query() -> None:
    q = "What happens if my CGPA stays below 2.00 for three consecutive semesters?"
    assert add_typos(q, 7) == add_typos(q, 7)
    assert add_typos(q, 7) != q
    assert add_typos("a b c", 1) == "a b c"


def test_percentile_nearest_rank() -> None:
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([], 50) is None


def test_citation_validity_and_correctness() -> None:
    resp = {"citations": [{"source": "h.pdf", "section": "4. Registration > Withdrawal from a Course"},
                          {"source": "made-up.pdf", "section": "X"}],
            "retrieved": [p("h.pdf", "4. Registration > Withdrawal from a Course")]}
    assert citation_validity(resp) == (1, 2)
    assert citation_correct(resp, GOLD) is True
    assert citation_correct({"citations": []}, GOLD) is None


def test_overlap_support_ignores_citations_and_stopwords() -> None:
    ctx = "4.25 The deadline to withdraw is the last day of the makeup week."
    assert overlap_support("The deadline is the last day of the makeup week. [h.pdf / 4.25]", ctx) == 1.0
    assert overlap_support("Attendance is optional and fees are waived.", ctx) < 0.3


def test_diff_metrics_walks_nested_numbers_only() -> None:
    a = {"e2e": {"faithfulness": 0.9, "judge": {"mode": "llm"}, "ok": True}, "n": 3}
    b = {"e2e": {"faithfulness": 0.8, "judge": {"mode": "llm"}, "ok": True}, "n": 3}
    assert diff_metrics(a, b) == {"e2e.faithfulness": pytest.approx(0.1), "n": 0.0}


def _result(r5: float, faith: float, abst: float, p95: float, cv: float = 1.0) -> dict:
    return {"component": {"clean": {"recall@5": r5}},
            "e2e": {"faithfulness": faith, "abstention_correctness": abst,
                    "citation_validity": cv, "latency_ms": {"p95": p95}}}


T = {"recall_at_5_max_drop": 0.02, "faithfulness_min": 0.8, "citation_validity_min": 0.95,
     "abstention_not_below_baseline": True, "p95_latency_ms_max": 180000}


def test_gate_passes_a_good_candidate_and_rejects_a_bad_one() -> None:
    base = _result(0.85, 0.9, 1.0, 120000)
    assert decide(evaluate(_result(0.86, 0.9, 1.0, 110000), base, T))
    bad = evaluate(_result(0.60, 0.9, 1.0, 110000), base, T)
    assert not decide(bad)
    assert [r["rule"] for r in bad if r["pass"] is False] == ["recall@5 drop vs baseline"]


def test_gate_outcome_follows_the_threshold_not_the_code() -> None:
    base, cand = _result(0.85, 0.9, 1.0, 120000), _result(0.84, 0.82, 1.0, 120000)
    assert decide(evaluate(cand, base, T))
    assert not decide(evaluate(cand, base, {**T, "faithfulness_min": 0.85}))
    assert not decide(evaluate(cand, base, {**T, "recall_at_5_max_drop": 0.005}))


def test_gate_without_baseline_skips_relative_rules() -> None:
    rows = evaluate(_result(0.85, 0.9, 1.0, 1000), None, T)
    assert {r["rule"]: r["pass"] for r in rows}["recall@5 drop vs baseline"] is None
    assert decide(rows)


def test_coverage_nearest_and_topic_grouping() -> None:
    golden = {"g1": [1.0, 0.0, 0.0], "g2": [0.0, 1.0, 0.0]}
    best_id, score = nearest([0.9, 0.1, 0.0], golden)
    assert best_id == "g1" and score > 0.9
    groups = group_topics([("a", [0.0, 0.0, 1.0], "hostel"), ("b", [0.0, 0.1, 0.99], "hostel"),
                           ("c", [0.7, 0.7, 0.0], "transport")], threshold=0.8)
    assert sorted(len(g["queries"]) for g in groups) == [1, 2]


def test_golden_set_meets_the_assignment_minimums() -> None:
    rows = [json.loads(line) for line in (HERE / "golden.jsonl").read_text(encoding="utf-8").splitlines()]
    cats = [r["category"] for r in rows]
    assert len(rows) >= 25
    assert cats.count("answerable") >= 15 and cats.count("unanswerable") >= 5
    assert cats.count("hard") >= 5
    assert sum(r["label"] == "human" for r in rows) >= 8
    assert sum(r["label"] == "synthetic" for r in rows) >= 8
    for r in rows:
        assert r["label"] in ("human", "synthetic")
        if not r["must_abstain"]:
            assert r["relevant"] and all(g["page"] for g in r["relevant"]), r["id"]
    pairs = [json.loads(line) for line in (HERE / "embedding_pairs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(x["kind"] == "paraphrase" for x in pairs) >= 15
    assert sum(x["kind"] == "unrelated" for x in pairs) >= 15
