# Failure analysis

We broke four stages on purpose, one at a time, and restored each one: **A, B, C, E**.
Retrieval numbers come from `eval/chunk_experiment.py` on the full corpus and all 21
golden questions that have relevant passages. Each broken variant was indexed into its
own scratch collection with the real indexer code, and queried with the api's own
`ChromaRetriever`. Raw results: `eval/experiments/*.json`.

Baseline (shipped: structure-aware 400 / 50, OCR on, superseded filter on, top_k 5):
**recall@1 0.595 · recall@3 0.881 · recall@5 0.881**, 690 chunks.

From the RAG lecture: only F is the model's fault, so check A to E first. The order of
diagnosis we use: **the per-request log line first** (`chunk_ids`, `scores`,
`abstained`), then `/metrics`, then the index itself.

---

## Stage A · Extraction: the scanned PDFs indexed without OCR

- **What we broke:** `OCR_ENABLED=false` for the extraction (`indexer/pipeline/extract.py`,
  `ocr_enabled()`). Pages with no text layer are then indexed as empty.
- **Symptom:** **89 chunks disappear** (690 → 601) and **recall@5 drops from 0.881 to
  0.786** (`eval/experiments/stage-a-no-ocr.json`). Exactly two questions fail:
  - **q11** ("How long must a faculty member have been full-time to start a technical
    start-up?"): its only source, the image-only start-up policy, is no longer in the
    index at all. The top passage becomes an unrelated prospectus clause.
  - **v04** ("Under the 2023 academic rules, what was the deadline to withdraw?"): the
    2023 rules are 24 scanned pages, so the "old version" side of the version pair is
    empty. Retrieval silently returns the **2026** "Withdrawal from a Course" instead.
    The system would quote the current rule as if it were the 2023 one. That is the
    most dangerous symptom: a confident, cited, wrong-version answer.
- **Diagnosis, as if we had not caused it:** the indexer's own log shows it at index
  time. Each document logs `"words"` and `"ocr_pages"`; without OCR,
  `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` logs `"words": 0` and
  `"chunks": 0`, and the 2023 rules drop from 4,869 words to 21 (the cover page). At query time, the
  log line for q11 shows `chunk_ids` from other documents only, with a lower top
  score. A document that should be cited never appears in any `chunk_ids`.
- **Stage:** A, extraction.
- **Remedy:** keep OCR on (the default). The extract stage OCRs any page with fewer
  than 50 characters, and the indexer log reports which pages were OCR'd, so a
  document with `"chunks": 0` is an alarm.

## Stage B · Chunking: fixed 100-word windows, no overlap

- **What we broke:** `CHUNK_STRATEGY=fixed`, `CHUNK_SIZE=100`, `CHUNK_OVERLAP=0`
  (`indexer/pipeline/chunk.py`, `chunk_fixed`).
- **Symptom:** **recall@5 0.881 → 0.643, recall@1 0.595 → 0.143**
  (`eval/experiments/chunking.json`, row "fixed 100 0"). The injection question i01
  ("What attendance is required in lectures?") drops to 0. Clause 1.21 is split
  across two windows, and neither half carries the heading "Attendance Requirements".
  The Generative AI policy's "Unacceptable
  Uses" paragraph is also cut away from its list of examples. q05 still retrieves a
  piece of it, but the LLM would see the examples without the rule they illustrate.
- **Diagnosis, as if we had not caused it:** the CI gate fails first: recall@5 more
  than 5 points below `eval/baseline.json`. In the logs, `scores` for the top
  passages are lower and flatter, and the retrieved `section` values stop matching
  the question's topic, because a window starts mid-clause and is labelled with the
  previous heading. Opening the chunk text shows sentences cut in half.
- **Stage:** B, chunking.
- **Remedy:** structure-aware chunking (ADR 0003). Never cross a section boundary,
  write the heading path into every chunk, and cut long blocks at sentence ends.

## Stage C · Retrieval: top_k 1, and EMBED_MODEL changed on the api only

**C1, `TOP_K: "1"`.**

- **What we broke:** `TOP_K=1` in the ConfigMap.
- **Symptom:** the best passage is right only **59.5%** of the time (recall@1 0.595, against
  recall@5 0.881). q02 ("CGPA below 2.00 for three semesters") needs two passages,
  "Academic Warning/Probation" and "Closure of Admission", so with one passage it can
  never be fully answered. Answers become partial or abstain more often.
- **Diagnosis:** the per-request log shows a single entry in `chunk_ids`, and
  `rahbar_abstentions_total` rises. In eval, recall@5 equals recall@1.
- **Remedy:** `TOP_K: "5"`. It costs nothing in latency, because only the best 3
  above the floor go into the prompt (`LLM_CONTEXT_PASSAGES`).

**C2, EMBED_MODEL changed on the api only** (the guardrail demo,
`docs/evidence/embedding-pin.txt`).

- **What we broke:** `kubectl patch configmap rahbar-config` with
  `EMBED_MODEL=all-minilm`, then `kubectl rollout restart deployment/api`, without
  re-running the indexer.
- **Symptom with our guardrail:** the new api pod stays **0/1 READY**. Its log says
  `"msg": "not ready", "failed": {"embedding": "embedding model mismatch: index built
  with 'nomic-embed-text' (dimension 768), api configured with
  EMBED_MODEL='all-minilm'..."}`. Because of `maxUnavailable: 0` the rollout stops
  there, and the three old pods keep answering correctly.
- **Without the guardrail:** questions would be embedded with a different model. With a
  different dimension (all-minilm is 384), Chroma would error on every query. With
  the same dimension, it would silently return the nearest chunks in an unrelated
  vector space: wrong passages, cited confidently, no error anywhere.
- **Diagnosis:** `kubectl get pods` (0/1), then `kubectl describe pod` (readiness probe
  503), then `kubectl logs`: the line names both models.
- **Remedy:** change `EMBED_MODEL` and re-run the indexer Job together, or roll the
  ConfigMap back. The pin lives in one ConfigMap key read by both.

## Stage E · Source: the superseded filter removed

- **What we broke:** retrieval with `include_superseded=True` for every question,
  i.e. without the `{"superseded": false}` metadata filter
  (`api/app/retrieval/chroma.py`).
- **Symptom:** the repealed 2023 rules come back right beside the 2026 ones, with
  almost the same score. For "What is the last date to withdraw from a course?" the
  2026 "Withdrawal from a Course" scores **0.7828** and the **2023** one **0.7802**.
  **2 of the 5 passages are repealed rules**, and the same happens for the summer
  semester questions (`eval/experiments/stage-e-top5.txt`). The LLM then sees "last
  day of classes" (2023) and "last day of the makeup week" (2026) side by side.
  recall@3 falls 0.881 → 0.833 as the old chunks push current ones down. A prompt
  that says "prefer the current version" helps only if the model reads the version
  labels correctly. The filter makes the wrong version impossible to retrieve.
- **Diagnosis, as if we had not caused it:** the per-request log line's `chunk_ids`
  contain `undergraduate-academic-rules-aug-2023-pdf-…` for a question that did not
  ask about 2023, and the answer's citation names the 2023 file. In eval,
  version-question answers stop containing the expected 2026 text (`expect_found`
  in `results.json`).
- **Stage:** E, source.
- **Remedy:** the metadata filter `where={"superseded": False}`, lifted only when the
  question explicitly asks for an older version (`wants_old_version()` in
  `services/grounding.py`, tested in `api/tests/test_pipeline.py`).

---

## Not broken here

- **D (prompt without the context instruction)** and **F (temperature 1.2)** are the
  generation-side failures. We did not run them, because each full evaluation with the
  CPU-only model takes about half an hour. Their guards are in place:
  - "Use the supplied context only" is rule 1 of the system prompt (`prompt.py`).
  - Temperature is 0.1 in the ConfigMap.
  - Whatever the model writes, citations are checked against the retrieved passages,
    and anything that is not valid Answer JSON is retried once and then replaced by
    the fallback.
