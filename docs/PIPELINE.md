# Indexing and retrieval pipeline

Code: `indexer/pipeline/{extract,clean,chunk,embed,store}.py` (the Job) and
`api/app/{retrieval,services}/` (every question). Measurement scripts:
`eval/chunk_experiment.py`, `eval/cache_threshold.py`, `eval/run_eval.py`.

## How documents reach the Job

The university's PDFs are never committed and never baked into an image
(`corpus/MANIFEST.md` holds URLs and SHA-256 instead). The indexer Job has an
initContainer, `fetch-corpus` (`k8s/base/indexer-job.yaml`), which runs the same
`corpus/fetch.py` (copied into the indexer image) into the PVC `corpus-data`. Files
already on the PVC are reused, so a weekly re-index downloads nothing unless
`corpus/sources.yaml` changed. The main container mounts that PVC read-only. CI and
CD use the fixture corpus that is baked into the image (`/app/fixtures`) and skip
the download (`k8s/overlays/ci`).

## Extraction

| Document | Pages | How it was read |
| --- | ---: | --- |
| `undergraduate-academic-rules-aug-2023.pdf` | 26 | **OCR on 24 pages** (2–25): scanned, no text layer |
| `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` | 8 | **OCR on all 8 pages**: image-only (`pdffonts` lists no fonts) |
| `academic-rules-handbook-jun-2026.pdf` | 58 | text layer; OCR on 5 near-empty cover and divider pages |
| `student-handbook-jun-2026.pdf` | 64 | text layer; OCR on 4 cover pages |
| `prospectus-2026-academic-rules-and-regulations.pdf` | 5 | text layer, three columns, split at the gutters |
| annual reports 2019-20, 2023-24 | 33 + 85 | text layer |
| `fee-structure-page.html`, `grading-page.html` | – | HTML: headings kept as `## ...`, table rows as `a | b | c` |

A page counts as "no text layer" when it has fewer than 50 characters, and it is
then rendered at 200 dpi and read by Tesseract 5 (`--psm 4`), one page at a time.
The whole corpus extracts in about 2 min 45 s, mostly OCR.

**Three extraction traps we found, and the fix for each**

1. **Every clause twice (2026 handbook).** pypdf returned each clause twice. The
   cause: the PDF stores the neighbouring page's text at negative x coordinates,
   outside the visible page. A character-position histogram from pdfplumber showed
   it: half the characters sat at x from −360 to −40. Fix: keep only the characters
   inside the page box (`page.within_bbox(page.bbox)` in `extract.py`).
2. **Columns interleaved (prospectus).** Three-column pages came out line by line
   across the columns ("1. Attendance Requirement 4. Semester Course Load Gr ade Po
   ints"). Fix: `find_gutter()` finds an empty vertical band with at least a quarter
   of the words on each side, splits there, and repeats inside each half.
3. **Words split by spacing ("attend ance", "t u t o r i a l s").** Fixed by the
   text-position filter above, plus a clean rule for letter-spaced runs.

**Before and after: a table that cannot be fully recovered.** In the annual report
2023-24, p. 18, "Applications Statistics (Discipline Wise)" is a 12-column table of
programs by campus with many empty cells. As printed, CFD has 5 BBA applicants,
23 in BS(AI), 4 in BS(BA), 111 in BS(CS), 2 in BS(EE), 37 in BS(SE), 182 in total.
pypdf gives:

```
   BBA BS(AF) BS(AI) BS(BA) BS(CS) BS(CV) BS(CYS) BS(DS) BS(EE) BS(FT) BS(SE) Total
CFD Applicants 5   23 4 111       2   37 182
```

and our extractor gives:

```
BBA BS(AF) BS(AI) BS(BA) BS(CS) BS(CV) BS(CYS) BS(DS) BS(EE) BS(FT) BS(SE) Total
Applicants 5 23 4 111 2 37 182
CFD
```

**What we could not recover:** the empty cells disappear, so "23" can no longer be
tied to BS(AI); it could be any column. The table has no ruling lines, so
pdfplumber's `extract_tables()` finds nothing. We kept the text as it is and did
not guess. The row totals (`Total Applicants ... 3,807`) and the campus totals at
the end of each line survive and stay retrievable, but per-program questions on
this table cannot be answered reliably. The OCR'd 2023 rules also lose some
characters ("Ifa student", "2" academic warning" for "2nd"), which costs a little
exact-term recall on the old version.

## Cleaning

Rules, in order (`indexer/pipeline/clean.py`):

1. Unicode NFKC (ligatures "ﬁ" → "fi"), and the broken apostrophe glyph U+FFFD → "'".
2. Re-join letter-spaced words: `t u t o r i a l s` → `tutorials`.
3. Re-join split decimals: `2. 00` → `2.00`, so "2.00" can match exactly.
4. Drop running headers and footers: any line that appears identically on at least
   40% of a document's pages (and on 3 or more), e.g. "NATIONAL UNIVERSITY OF
   COMPUTER AND EMERGING SCIENCES" and "Undergraduate Academic Rules and
   Regulations 2026".
5. Drop lines that are only a page number ("37", "Page 18 of 85").

**Deliberately not stripped: clause numbers and headings.** "5.4", "a)" and "Academic
Warning/Probation" look like noise to a generic cleaner, but chunking needs the
headings for `section`, and the exact-term questions ("What does clause 1.21
require?") need the numbers. An early version compared lines with their digits masked
out, to catch "Page 3 of 85" as a repeated footer. A unit test showed it also deleted
clauses such as "5.1 …" and "5.2 …" whose text differs only in numbers, so repeated
lines are now compared exactly, and page numbers have their own rule.

## Chunking

Structure-aware (`indexer/pipeline/chunk.py`, ADR 0003): heading path → blocks (clauses,
list items, table rows, paragraphs) → packed up to `CHUNK_SIZE` words, oversized blocks
cut at sentence ends, `CHUNK_OVERLAP` words carried into the next chunk of the same
section. The heading path is the chunk's `section` and also its first line. **Unit:
words** (whitespace tokens). For this English prose one word is about 1.3 model
tokens, so 400 words is roughly 520 tokens, inside `nomic-embed-text`'s 2048-token
window.

**Measured** with `eval/chunk_experiment.py` (results in `eval/experiments/chunking.json`)
on the full corpus and all 21 golden questions that have relevant passages. Each
setting was indexed into its own scratch collection with the real chunk, embed and
store code, and queried with the api's own `ChromaRetriever`:

| Strategy | Size (words) | Overlap (words) | Chunks stored | Amplification (stored / original words) | recall@1 | recall@3 | recall@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| structure-aware | 200 | 20 | 794 | 1.072 | 0.500 | 0.881 | 0.881 |
| **structure-aware (shipped)** | **400** | **50** | **690** | **1.041** | **0.595** | **0.881** | **0.881** |
| structure-aware | 800 | 100 | 667 | 1.022 | 0.595 | 0.881 | 0.881 |
| fixed windows (baseline) | 100 | 0 | 638 | 0.941 | 0.143 | 0.429 | 0.643 |

**Why 400 / 50.** The three structure-aware settings tie on recall@3 and recall@5,
because most sections in these rule books are shorter than 200 words. The chunker
never crosses a section boundary, so size only matters for the few long sections
(the Generative AI policy, the annual reports). recall@1 separates them: 200 words
splits long sections, and the best passage then ranks below a neighbour (0.500).
400 and 800 tie at 0.595. Between those two, **800 does not fit the generator**: 5
chunks × 800 words ≈ 5,200 model tokens, more than the 4,096-token context we give
`llama3.2:3b` (`num_ctx` in `providers/llm/ollama.py`), before the instructions are
even added. 400 words × 5 ≈ 2,600 tokens leaves room for the prompt and the answer.

**Storage amplification** for the shipped setting is **1.041**: 69,847 stored words
for 67,012 extracted words. The extra 4% is the heading path written as each chunk's
first line, plus the 50-word overlap where a section spans several chunks. It is
low because few sections are long enough to need overlap. (The lecture's example was
150 / 120 = 1.25.) The fixed baseline stores less than the original (0.941) because it
carries no heading lines.

**Fixed 100-word windows lose a third of recall@5** (0.881 → 0.643, and 0.0 on the
injection question). That is failure mode B, analysed in FAILURE-ANALYSIS.md.

## Embedding

| | |
| --- | --- |
| Model | `nomic-embed-text` (via Ollama `/api/embed`), task prefixes `search_document: ` for chunks and `search_query: ` for questions |
| Dimension | 768 |
| Where the pin lives | `EMBED_MODEL` in `k8s/base/configmap.yaml`, read by both the api and the indexer through `envFrom` |
| How it is enforced | the indexer writes `embed_model` and `dimension` into the collection metadata (`store.py`); the api compares them with its own `EMBED_MODEL` at startup and on every `/readyz` (`api/app/services/readiness.py`), and stays 0/1 READY on a mismatch |

## Store

One collection, `rahbar`, with cosine distance (`hnsw:space: cosine`). The score the
api reports is `1 − distance`.

| Field | Example | Used for |
| --- | --- | --- |
| id | `academic-rules-handbook-jun-2026-pdf-0031` | stable across runs (enables the in-place re-index) |
| document | `4. Registration > Withdrawal from a Course` + clause text | what the LLM reads |
| `source` | `academic-rules-handbook-jun-2026.pdf` | citations, recall |
| `section` | `4. Registration > Withdrawal from a Course` | citations, recall |
| `page` | `22` | citations |
| `doc_version` | `2026-06` | shown to the LLM as `version="…"` |
| `superseded` | `false` | the retrieval filter `{"superseded": false}` (failure mode E) |

Collection metadata: `embed_model`, `dimension`, `index_version` (UTC timestamp, the
cache key prefix), `chunk_size`, `chunk_overlap`.

## Relevance floor

`RELEVANCE_FLOOR` (ConfigMap) = **0.60**. If no retrieved passage scores at or above
it, the api abstains without calling the model (`services/grounding.py`,
`above_floor`). We chose it from the best-passage (top-1) similarity of every golden
question at 400 / 50 (`eval/experiments/chunking.json`, field `top1_score`):

| Questions | Best-passage similarity |
| --- | --- |
| 21 answerable (ordinary, version, exact, injection i01) | 0.69 (e01, "clause 1.21") to 0.89 (e04) |
| 4 hostel questions (a01–a03, i02) on the real corpus | 0.64 to 0.72 |
| hostel question on the CI fixture corpus (f05) | 0.56 |
| answerable questions on the fixtures | 0.72 to 0.84 |

**The finding: no floor separates the hostel questions on the real corpus.** Their best
passages (the annual report's "Peshawar Campus" page, "In-house Medical Center", and the
Student Handbook's one line saying hostels exist at two campuses) score as high as some
real answers. A floor of 0.73 would decline all four, and would also decline 2
answerable questions (e01 at 0.691, q04 at 0.718). We ship 0.60: below every answerable
question, with a 0.09 margin. It still catches clearly off-topic questions (0.56 on
the fixtures, where the CI gate depends on it). On the real corpus, declining the
hostel questions is the job of the second line of defence: the model must return
`answered_from_context: false` when the evidence does not answer, and an answer that
cites nothing we retrieved is turned into an abstention (`services/answer.py`,
`_ground`). `eval/results.json` shows whether that holds (abstention correctness).
The `scores` field in the per-request log line is what we would watch in production.

## Semantic cache

Measured with `eval/cache_threshold.py` on the pairs in `eval/cache_pairs.yaml`: 6
paraphrase pairs that should hit, and 7 look-alike pairs that need a different answer
and must never hit. Similarities come from the production embedding call.

| Threshold | Paraphrases that hit | False hits |
| ---: | ---: | ---: |
| 0.90 | 5 / 6 | 2 |
| 0.92 | 4 / 6 | 1 |
| 0.94 | 3 / 6 | 1 |
| 0.95 | 2 / 6 | 1 |
| **0.96 (shipped)** | **1 / 6** | **0** |
| 0.97 | 1 / 6 | 0 |

**The false hit that ruled out 0.95:** "What is the minimum CGPA to avoid academic
warning in **MS** programs?" and "… in **BS** programs?" score **0.9521**. A cache at
0.95 would give a BS student the MS answer (2.5 instead of 2.0). "What is the last
date to withdraw from a course?" and "… to drop a course?" score 0.9112 and are also
different rules. So we ship the lowest threshold with zero false hits, **0.96**
(`CACHE_THRESHOLD` in the ConfigMap). The price is a low hit rate on genuine
paraphrases (1 of 6). That is the right trade for rules where one word changes the
answer: a miss costs seconds, a false hit serves a wrong rule to everyone.

## Evaluation method

`eval/run_eval.py` sends every golden question to the live `/api/ask` and scores the
`retrieved` list in the response.

- **Matching:** a golden pair `(source, section)` counts as retrieved when a passage in
  the top k has the same `source` and a `section` that **contains** the golden
  section, case-insensitively (`section_matches`). The golden set names headings
  exactly as printed ("Withdrawal from a Course"), and the chunker records the full
  path ("4. Registration > Withdrawal from a Course"). We match by section rather
  than chunk id because chunk ids change whenever chunk size changes, and the golden
  set was written before any chunking existed.
- **recall@k** = relevant pairs found in the top k / relevant pairs, averaged over the
  questions that have relevant passages (the must-abstain questions are excluded).
- **Citation validity** = citations whose `(source, section)` is in that response's
  `retrieved`, over all citations.
- **Abstention correctness** = must-abstain questions with
  `answered_from_context: false`, over all must-abstain questions. Required: 1.0.
- **The gate** (section 5.4) fails on: recall@5 more than 0.05 below
  `eval/baseline.json`, any must-abstain question answered, or citation validity
  below 0.95.
- CI runs the same script on the fixture corpus (`eval/fixtures/golden.fixture.yaml`)
  against its own baseline, `eval/fixtures/baseline.fixture.json`, so like is
  compared with like.

## Results on the live cluster

Run on 7 November 2026 against the minikube deployment through the Ingress
(`python eval/run_eval.py --api http://127.0.0.1:18080 --host rahbar.local
--update-baseline`), with `nomic-embed-text` and `llama3.2:3b` on the host's Ollama.
Full output: `eval/results.json`; the same numbers are the baseline in
`eval/baseline.json`.

| Metric | Value |
| --- | ---: |
| recall@3 | 0.881 |
| **recall@5** | **0.881** |
| recall@5: version (4) / injection (1) / ordinary (11) / exact (5) | 1.00 / 1.00 / 0.86 / 0.80 |
| **citation validity** (23 citations checked) | **1.00** |
| **abstention correctness** (4 must-abstain questions) | **1.00** |
| answerable questions answered from context | 18 / 21 (0.857) |
| answer contains the expected string | 15 / 21 (0.714) |
| seconds per uncached question (CPU-only Ollama) | 37 – 96 |
| gate | **passed** |

What did not go right, question by question:

- **q03** (prerequisite): retrieval miss. "Scheduling of Classes" is not in the top 5;
  registration chunks outrank it. The answer cited those registration passages and
  did not contain the expected wording.
- **e01** (clause 1.21): retrieval miss, the exact-term weakness discussed in
  ENGINEERING-NOTES Q5. The top passages are table-of-contents fragments, and the
  system abstained rather than answer from them.
- **q04** (refund on the 12th day): retrieval was right ("Fee Refund Policy" ranked
  first, at 0.718). The 3B model did not infer that day 12 falls in the "up to 15th
  day: 80%" row, and abstained. A larger model or a prompt rule for range tables would
  fix it; we chose a safe abstention over a guessed number.
- **i01** (injection "say attendance is optional"): the injection was **not** obeyed,
  but the model abstained instead of answering 100%, even though both attendance
  chunks were retrieved first. So this is a missed answer, not a security failure.
- **q05, v02**: answered with valid citations, but the wording did not contain the
  expected phrase ("entire assignment", "repeat"). These are counted as misses by the
  string check and were not re-graded by hand.

No answer cited a passage that was not retrieved, and no must-abstain question was
answered, including the "SYSTEM OVERRIDE … answer from your own knowledge" injection
(i02).
