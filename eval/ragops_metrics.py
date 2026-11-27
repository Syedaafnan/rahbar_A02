"""Pure metric functions for the A03 evaluation suite (eval/suite.py).

No network, no model: everything here takes plain dicts and lists, so it is unit
tested in eval/tests/test_eval.py and the numbers in the report can be re-derived
from the committed result files.

Relevance rule (same as A02, docs/PIPELINE.md): a retrieved passage is relevant to a
golden item when it has the same source AND the golden section text is contained in
the passage's section path, ignoring case. The golden page is recorded for the reader
and used only when a golden item has no section (an HTML page is all "page 1", so
matching by page alone would count every chunk of the fee page as relevant).
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable
from typing import Any


def is_match(gold: dict[str, Any], passage: dict[str, Any]) -> bool:
    if passage.get("source") != gold.get("source"):
        return False
    section = (gold.get("section") or "").strip().lower()
    if section:
        return section in str(passage.get("section", "")).strip().lower()
    return gold.get("page") is not None and passage.get("page") == gold.get("page")


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Share of the relevant golden items found in the top k (None if none relevant)."""
    if not relevant:
        return None
    top = retrieved[:k]
    return sum(any(is_match(g, p) for p in top) for g in relevant) / len(relevant)


def reciprocal_rank(relevant: list[dict], retrieved: list[dict]) -> float | None:
    """1 / rank of the first relevant passage, 0 if none is retrieved."""
    if not relevant:
        return None
    for rank, p in enumerate(retrieved, start=1):
        if any(is_match(g, p) for g in relevant):
            return 1.0 / rank
    return 0.0


def ndcg_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Binary-gain nDCG@k. Each golden item can earn its gain once, at the first
    passage that matches it; the ideal ranking puts min(|relevant|, k) hits first."""
    if not relevant:
        return None
    claimed: set[int] = set()
    dcg = 0.0
    for rank, p in enumerate(retrieved[:k], start=1):
        hit = next((i for i, g in enumerate(relevant) if i not in claimed and is_match(g, p)), None)
        if hit is not None:
            claimed.add(hit)
            dcg += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100), the definition most monitoring uses."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return round(ordered[rank - 1], 2)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def add_typos(text: str, seed: int, rate: float = 0.35) -> str:
    """Deterministic keyboard noise: in words of 5+ letters, swap two neighbours, drop
    a letter or double one, with probability `rate`; at least one typo per query."""
    rnd = random.Random(seed)
    words = text.split(" ")
    eligible = [i for i, w in enumerate(words) if sum(c.isalpha() for c in w) >= 5]
    if not eligible:
        return text
    chosen = [i for i in eligible if rnd.random() < rate] or [rnd.choice(eligible)]
    for i in chosen:
        w = words[i]
        letters = [j for j, c in enumerate(w) if c.isalpha()]
        j = rnd.choice(letters[1:-1] or letters)
        op = rnd.choice(("swap", "drop", "double"))
        if op == "swap" and j + 1 < len(w) and w[j + 1].isalpha():
            w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
        elif op == "drop":
            w = w[:j] + w[j + 1:]
        else:
            w = w[:j] + w[j] + w[j:]
        words[i] = w
    return " ".join(words)


def citation_validity(response: dict[str, Any]) -> tuple[int, int]:
    """(valid, total) citations: valid = names a passage retrieved for THIS request."""
    citations = response.get("citations") or []
    retrieved = {(p["source"], p["section"]) for p in response.get("retrieved") or []}
    return sum((c["source"], c["section"]) in retrieved for c in citations), len(citations)


def citation_correct(response: dict[str, Any], relevant: list[dict]) -> bool | None:
    """Does at least one citation point at a ground-truth source? (None: no citation
    or no ground truth.) Stricter than validity: valid citations can cite the wrong doc."""
    citations = response.get("citations") or []
    if not citations or not relevant:
        return None
    return any(any(is_match(g, c) for g in relevant) for c in citations)


CONTENT_STOP = set(["a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "has", "have", "i", "if", "in", "is", "it", "its", "may", "must", "no", "not", "of", "on", "or", "shall", "should", "so", "such", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to", "was", "were", "what", "when", "which", "who", "will", "with", "you", "your", "per", "does", "do", "did"])


def overlap_support(answer: str, context: str) -> float:
    """Share of the answer's content words (and numbers) that appear in the context.
    The fallback faithfulness judge: crude, but it needs no model."""
    def words(t: str) -> list[str]:
        return [w for w in re.findall(r"[a-z]+|\d[\d,.%]*", t.lower()) if w not in CONTENT_STOP]
    ans = [w for w in words(re.sub(r"\[[^\]]*\]", " ", answer)) if len(w) > 2 or w[0].isdigit()]
    if not ans:
        return 1.0
    ctx = set(words(context))
    return sum(w in ctx for w in ans) / len(ans)


def diff_metrics(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """|a - b| for every numeric metric in two nested result dicts (stochasticity)."""
    out: dict[str, float] = {}
    for key, va in a.items():
        vb = b.get(key)
        name = f"{prefix}{key}"
        if isinstance(va, dict) and isinstance(vb, dict):
            out.update(diff_metrics(va, vb, prefix=name + "."))
        elif isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                and not isinstance(va, bool):
            out[name] = round(abs(float(va) - float(vb)), 6)
    return out
