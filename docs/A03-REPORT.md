# CS4032 Assignment 03: RAGOps for RAHBAR

**Group members:** Syeda Afnan (24i-3089) · Arooj Abrar (24i-3170)  
**Repository:** https://github.com/Syedaafnan/rahbar_A02 · **Reference:** Xu et al., *RAGOps* (2026)

RAHBAR answers FAST-NUCES students' questions from university documents with citations and abstains when the documents do not cover a question (A02). A03 adds the paper's RAGOps layer on minikube: ingestion, evaluation, gate and coverage run as Jobs; the data lake, traces and results live on PVCs; thresholds are ConfigMaps. Setup: CPU-only laptop, Ollama on the host (`nomic-embed-text` live, `mxbai-embed-large` candidate, `all-minilm` bad candidate, `llama3.2:3b` for answers and the judge, seed 42). Corpus: 9 FAST-NUCES documents (690 chunks) plus Batch B (12 synthetic documents with planted problems). All numbers come from our runs (`eval/results/`, `traces/`).

## 1. Figure 3 stages in our pipeline

| Stage | What our pipeline does | File(s) | What is missing |
| --- | --- | --- | --- |
| Ingest | Fetch corpus, discover batches in `lake/raw/`, read metadata, extract (OCR) and clean, cached by SHA-256 | `corpus/fetch.py`, `indexer/pipeline/lake.py`, `run.py` | No push trigger; batches copied with `scripts/lake.py` |
| Verify | Quality, completeness, recency, uniqueness per document; report per run | `indexer/pipeline/verify.py` | No injection or PII check at ingest |
| Update data lake | Quarantine failures, archive superseded versions, registry of hashes and chunk ids | `lake.py`, `run.py` | No human review of quarantine |
| Update retrieval sources | Incremental sync (embed only new chunk ids, delete stale), archive by metadata, restore/unpin | `indexer/pipeline/store.py` | No blue/green swap of collections |
| Offline testing | Module, component and end-to-end tests on 36 golden questions, seeded, run twice; release gate | `eval/suite.py`, `eval/gate.py`, `k8s/eval-*.yaml` | Judge is a 3B model |
| Live testing | One trace per query, Prometheus metrics, guardrail actions, trace summary and trace tool | `api/app/services/trace.py`, `scripts/trace_*.py` | No alerts or dashboard |
| Check coverage | Live queries vs golden questions, share above threshold, uncovered topics, exit 1 below 85% | `eval/coverage_check.py`, `k8s/coverage-job.yaml` | Live queries written by us |

## 2. The five qualities (paper Table 2)

| Quality | What we measure | Threshold | Evidence |
| --- | --- | --- | --- |
| Adaptability | Chunks embedded per change; rollback cost | 0 on unchanged corpus | A2: 0 of 719 re-embedded; rollback = 16 metadata updates |
| Monitorability | Top-1 similarity, abstention, cache hits, p95 per stage, injection flags | coverage ≥ 85% | D3 summary, `/metrics` |
| Observability | One JSON trace per query with retrieval, prompt hash, params, latency | 100% traced | `traces/*.jsonl`, `kubectl logs` |
| Traceability | Rebuild the exact prompt from a trace id | prompt hash matches | D2, D5 |
| Reliability | Gate-blocked release, rolling update, rollout undo, PVCs survive restarts | gate rules in ConfigMap | C2, F3; cluster restarted twice, data intact |

## 3. Results

### Part A: verification and incremental update

Base corpus: 9 documents, 8 indexed, the 2023 rules archived (recency), 690 chunks. Batch B (`eval/results/A1-ingest-batch-b.log`):

| Document | Planted problem | Check that fired | Decision |
| --- | --- | --- | --- |
| b02, b04 | exact duplicates of b01, b03 | uniqueness (same SHA-256) | quarantine |
| b05 | near-duplicate of b01 | uniqueness (cosine 0.995 ≥ 0.96) | quarantine |
| b06 | truncated PDF | quality + completeness | quarantine |
| b07 | no metadata | completeness | quarantine |
| b11, b12 | garbled OCR, empty | quality | quarantine |
| b08 | newer fee page | old page fails recency | b08 indexed, old archived |
| b09 | injected instruction | none (by design) | dropped at query time (D4) |

Incremental update (A2): an unchanged re-run embedded **0** of 719 chunks; a changed document (b10) replaced only its 2 chunks; deleting b03 removed its 6 chunks with **0 orphaned documents** (`--count-by-doc`); restoring the archived fee page relabelled 16 chunks with no re-embedding, and unpinning returned the newest version. Distinct documents reach cosine 0.956 at most, so the 0.96 near-duplicate threshold works but with a thin margin.

### Part B: offline tests

Golden set: 36 questions (21 answerable, 7 unanswerable, 8 hard; 25 human, 11 synthetic, reviewed by hand: **TBD**). Baseline after Batch B, two seeded runs (`20261126T064121Z-baseline.json`):

| Level | Metric | Run 1 | \|run1 − run2\| |
| --- | --- | ---: | ---: |
| module | paraphrase / unrelated cosine, gap | 0.757 / 0.387, 0.370 | 0 |
| module | embedding ms per chunk; determinism | 841 ms; max diff 0.0 | 161 ms; 0 |
| component | recall@3 / recall@5 / MRR / nDCG@5 | 0.776 / 0.776 / 0.707 / 0.718 | 0 |
| component | recall@5 with typos | 0.672 (−0.104) | 0 |
| end-to-end | faithfulness / abstention correctness | 0.962 / 1.000 | 0 |
| end-to-end | citation validity / correctness | 1.000 / 0.885 | 0 |
| end-to-end | latency p50 / p95 | 73.0 s / 123.1 s | 14.0 s / 28.4 s |

Stochasticity: all quality metrics were identical across runs; one answer (q06) changed wording, because CPU thread scheduling can flip near-tied tokens even with a fixed seed. Latency varies with laptop load. Judge: our first prompt gave 0.08 because the 3B judge rejected answers stated word for word in the context; the fixed judge sees the question and must quote evidence found in the context. Judge vs our hand labels: **TBD** (`python eval/label_answers.py <result> --who NAME`, then `--report`).

### Part C: change experiment and release gate

Change: embedding model `nomic-embed-text` → `mxbai-embed-large`, own collection. Bad candidate: `all-minilm` with fixed 100-word chunks.

| Metric | Baseline | Candidate | Bad |
| --- | ---: | ---: | ---: |
| module gap | 0.370 | 0.477 | 0.595 |
| recall@3 / recall@5 | 0.776 / 0.776 | 0.724 / 0.793 | 0.224 / 0.379 |
| MRR | 0.707 | 0.693 | 0.226 |
| faithfulness / abstention | 0.962 / 1.000 | 0.920 / 1.000 | not run |
| citation correctness | 0.885 | 0.760 | – |
| latency p95 | 123.1 s | 72.4 s | – |
| **gate** | – | **PASS** | **FAIL** (recall@5 −0.397) |

Gate (`eval/gate.yaml` = ConfigMap `rahbar-gate`): recall@5 drop ≤ 0.02, faithfulness ≥ 0.80, citation validity ≥ 0.95, abstention ≥ baseline, p95 ≤ 180 s (our CPU baseline plus head room). The bad candidate was rejected on retrieval alone, so its answer level was skipped.

**Release decision: released, then rolled back.** The candidate passed every gate rule and was rolled out. Reviewing the full comparison showed what the gate does not check: citation correctness fell from 0.885 to 0.760 and recall@3 fell, which matters because only the top 3 passages reach the LLM. We reversed it with `kubectl rollout undo` and would add recall@3 and citation correctness to the gate.

### Part D: observability

D1/D2: each query writes one JSON trace to stdout and the `traces` PVC; `scripts/trace_show.py <id>` rebuilds the exact prompt and verifies its SHA-256. D3 (`trace_summary.py --since 30m`): top-1 similarity median 0.757, live abstention rate 0.10, cache hit rate 0.30, p95 generate 72.0 s of 72.3 s total, 4 injection attempts flagged. D4: a question about rescheduling exams retrieved b09's injected chunk first (0.821); the guardrail dropped it and logged the action, chunk id and matched pattern in the trace, and the answer followed the real rule.

D5, root cause from the trace only (e01, "What does clause 1.21 require?", abstained):
1. The five retrieved chunks (0.666–0.691) are headings and the table of contents.
2. The relevant chunk (Attendance, handbook p. 10) is missing.
3. The filter is not the cause: all results are the current 2026-06 version.
4. Dense embeddings ignore the clause number "1.21" and matched "academic rules".
5. Fix: a keyword (BM25) leg or clause-number metadata, not a prompt change.

### Part E: drift and coverage

42 live queries (28 in domain, 14 outside). Threshold: cosine ≥ 0.68, the least similar of our 15 paraphrase pairs (most similar unrelated pair: 0.445). Coverage **61.9% → exit 1**; uncovered topics: generative AI, fees, grading, attendance, registration (in domain); campus services, scholarships, health, sports, transport (outside). After 12 new golden questions: **90.5% → exit 0**.

Data drift (before vs after Batch B): recall@5 rose 0.707 → 0.776 because the 5 Batch B questions became answerable, but answerable recall fell 0.881 → 0.738: the fee questions' ground truth names the page Batch B archived. The golden set must be updated when data is superseded (paper 4.2.7).

### Part F: Kubernetes

F1: `api` Deployment (3 replicas, Service, Ingress), `chroma` StatefulSet, PVCs for the data lake, traces and results; all survived two cluster restarts. F2: ingestion runs as Job `indexer` and CronJob `reindex` (`concurrencyPolicy: Forbid`); eval Jobs end Failed when the gate fails; patching ConfigMap `rahbar-gate` flipped the same result from PASS to FAIL without a rebuild. F3: `promote bad` was refused; `promote good` rolled out with `kubectl rollout status` (maxUnavailable 0); after `kubectl rollout undo` a trace showed `rahbar` / `nomic-embed-text` again. F4: traces via `kubectl logs` and the PVC; `/metrics` via port-forward. F5 (`kubectl top`):

| Component | Measured | Request | Limit |
| --- | --- | --- | --- |
| api (per pod) | 4–35m CPU, 77 Mi | 100m, 160Mi | 500m, 384Mi |
| chroma | 2–38m CPU, 195 Mi | 100m, 256Mi | 1 CPU, 768Mi |
| indexer Job | peak 1.26 CPU, 523 Mi | 500m, 512Mi | 2 CPU, 1536Mi |
| eval Job | 5m CPU, 24 Mi | 50m, 64Mi | 500m, 256Mi |

## 4. Short answers

**(a)** Uniqueness and quality caught 3 problems each. We did not catch the injected instruction in b09, which is well-formed and unique; the query-time guardrail catches it, and running the same patterns over chunks at ingest would quarantine it earlier.

**(b)** The bad candidate had the best module-level gap (0.595 vs 0.370) yet the worst retrieval (recall@5 0.379 vs 0.776): short clean test pairs do not predict ranking long policy chunks. Similarly, mxbai improved the gap and recall@5 but lowered citation correctness.

**(c)** We archive: superseded versions keep their vectors, excluded by a metadata filter, plus a file copy. Deleting would break rollback (re-download and re-embed), questions about old rules, and old traces that point to deleted chunks.

**(d)** Faithfulness: the judge is a 3B model, and changing only its prompt moved the score from 0.08 to 0.88. Agreement with our hand labels: **TBD**. "Expected fact found" is also weak: it marks "15" wrong when the golden answer is "fifteen".

**(e)** Evolving the test set with the data: after Batch B three golden questions silently became wrong. We changed the paper's approach by calibrating the coverage threshold on our own paraphrase pairs, keeping old versions in the same collection (rollback is a metadata flip), and replacing online A/B testing with an offline gate, since one CPU cannot serve two releases.

**(f)** The indexer Job and CronJob (`Forbid`) are the only writers to Chroma. PVC `corpus-data` is the data lake; the `chroma` StatefulSet holds the retrieval sources; the `api` Deployment serves queries and writes traces; `api-candidate` serves the candidate; eval Jobs run the suite and fail on a failed gate; ConfigMap `rahbar-gate` holds thresholds; switching ConfigMap `rahbar-config` to `rahbar-config-candidate` is the release, so `kubectl rollout undo` restores the previous collection.

## 5. Contributions and AI statement

| Member | Contribution |
| --- | --- |
| Syeda Afnan (24i-3089) | Part A (verification stage, data lake, incremental update, Batch B); Part D (trace record, guardrail logging, trace tools, D5 root cause); Part F (manifests, ingestion Job and CronJob, controlled release and rollback, resources) |
| Arooj Abrar (24i-3170) | Part B (golden set, three-level evaluation suite, LLM judge, stochasticity); Part C (candidate experiment, release gate and its ConfigMap); Part E (live queries, coverage check, data drift) |
| Both | Hand labels for the judge, review of synthetic golden questions, screenshots, report, demo |

**AI statement:** _fill in (2–3 lines)._

## Appendix: screenshots

**A1: Verification report for Batch B (pass/fail per check, with reasons)**

![A1](screenshots/A1-verification-report-batch-b.png)

**A2: Second ingestion run on an unchanged corpus: 0 chunks embedded**

![A2](screenshots/A2-unchanged-corpus-0-embedded.png)

**B: Evaluation summary with all three levels (baseline, two seeded runs)**

![B](screenshots/B-evaluation-summary-three-levels.png)

**C2: Release gate failing (deliberately bad candidate)**

![C2](screenshots/C2-gate-fail-bad-candidate.png)

**C2: Release gate passing (mxbai candidate)**

![C2](screenshots/C2-gate-pass-candidate.png)

**D1: One full trace record**

![D1](screenshots/D1-full-trace-record.png)

**D2 (1/2): Trace tool rebuilding the prompt for a trace id**

![D2 (1/2)](screenshots/D2-trace-tool-rebuilds-prompt-1of2.png)

**D2 (2/2): Trace tool, rest of the same output**

![D2 (2/2)](screenshots/D2-trace-tool-rebuilds-prompt-2of2.png)

**E2: Coverage before the new golden questions**

![E2](screenshots/E2-coverage-before.png)

**E4: Coverage after the new golden questions**

![E4](screenshots/E4-coverage-after.png)

**F1 (1/2): kubectl get all,pvc,configmap,cronjob,job**

![F1 (1/2)](screenshots/F1-kubectl-get-all-1of2.png)

**F1 (2/2): kubectl get all,pvc,configmap,cronjob,job (rest)**

![F1 (2/2)](screenshots/F1-kubectl-get-all-2of2.png)

**F2: Logs of a completed ingestion Job**

![F2](screenshots/F2-completed-ingestion-job-logs.png)

**F2: Logs of a completed evaluation Job**

![F2](screenshots/F2-completed-evaluation-job-logs.png)

**F2: Gate result changing after a ConfigMap edit**

![F2](screenshots/F2-gate-changes-after-configmap-edit.png)

**F3: Controlled release: kubectl rollout status**

![F3](screenshots/F3-release-kubectl-rollout-status.png)

**F3: kubectl rollout undo, then a trace with the previous collection and embedding version**

![F3](screenshots/F3-rollout-undo-and-trace.png)
