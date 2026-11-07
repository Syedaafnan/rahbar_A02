#!/usr/bin/env python3
"""Choose CACHE_THRESHOLD with data (section 3.3).

The lecture's rule: the LOWEST threshold with ZERO false hits on our test questions.
Embeds every pair in eval/cache_pairs.yaml with the api's own embed_query (same model
and prefix as production), then reports for each threshold how many genuine paraphrases
would hit (good) and how many different questions would hit (bad).

    python eval/cache_threshold.py              # Ollama on localhost:11434
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

import httpx  # noqa: E402
import yaml  # noqa: E402
from app.config import Settings  # noqa: E402
from app.embedding import cosine, embed_query  # noqa: E402

THRESHOLDS = [0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--pairs", type=Path, default=ROOT / "eval" / "cache_pairs.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "experiments" / "cache.json")
    args = parser.parse_args()

    settings = Settings(ollama_url=args.ollama)
    pairs = yaml.safe_load(args.pairs.read_text(encoding="utf-8"))
    scored: dict[str, list[dict]] = {"same": [], "different": []}
    with httpx.Client() as client:
        for kind in ("same", "different"):
            for a, b in pairs[kind]:
                sim = cosine(embed_query(a, settings, client), embed_query(b, settings, client))
                scored[kind].append({"a": a, "b": b, "similarity": round(sim, 4)})
                print(f"{kind:9} {sim:.4f}  {a}  ||  {b}")

    table = []
    for t in THRESHOLDS:
        true_hits = sum(1 for p in scored["same"] if p["similarity"] >= t)
        false_hits = [p for p in scored["different"] if p["similarity"] >= t]
        table.append({"threshold": t, "true_hits": true_hits, "of_same": len(scored["same"]),
                      "false_hits": len(false_hits),
                      "false_hit_example": false_hits[0] if false_hits else None})
    safe = [row["threshold"] for row in table if row["false_hits"] == 0]
    chosen = min(safe) if safe else None

    print("\n| Threshold | Paraphrases that hit | False hits | Example false hit |")
    print("| ---: | ---: | ---: | --- |")
    for row in table:
        ex = row["false_hit_example"]
        example = f'"{ex["a"]}" -> "{ex["b"]}" ({ex["similarity"]})' if ex else ""
        print(f'| {row["threshold"]} | {row["true_hits"]} / {row["of_same"]} | '
              f'{row["false_hits"]} | {example} |')
    print(f"\nlowest threshold with zero false hits: {chosen}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"pairs": scored, "table": table, "chosen": chosen}, indent=2),
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
