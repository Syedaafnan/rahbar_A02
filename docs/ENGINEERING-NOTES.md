# Engineering notes

Answers point to files and line numbers in this repository. Evidence files live in
`docs/evidence/`.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

**The failure:** three writers on one index. `api.yaml:8` asks for `replicas: 3`, and the
HPA (`api.yaml`, `minReplicas: 3`) keeps it there. If indexing ran in startup, all three
pods would start together. Each would delete and rebuild the collection `rahbar` in
the one Chroma behind `chroma-data-chroma-0` (`chroma-statefulset.yaml:77`) while the
others are halfway through theirs. The result is an index that no single run
produced: missing or duplicated chunks, or chunks from a run that later crashed.
Every rolling update (`maxSurge: 1`, `api.yaml:15`) would start a fourth writer, and
every OOMKill or probe restart would pay the full OCR and embedding bill again
(7 min 41 s and up to ~526 Mi per run on our laptop, see Q4).

The startup probe makes it worse. `api.yaml:67-70` gives a pod 30 × 2 s = 60 s to start.
Indexing takes minutes, so the kubelet would kill every pod mid-index, and the rollout
would never finish.

**The object that prevents it:** the Job `indexer` (`indexer-job.yaml:19`, with
`restartPolicy: OnFailure` at line 30 and `backoffLimit: 2` at line 24) is the only
writer. The CronJob `reindex` (`reindex-cronjob.yaml`) has the same template, and
`concurrencyPolicy: Forbid` at line 13 means two indexers never overlap. The api code
contains no write call at all: `api/app/retrieval/chroma.py` only uses
`get_collection`, `query` and `count`. Re-indexing is
`kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar`, with no
redeploy.

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

Today, liveness is `/healthz` (`api.yaml:72-76`, `routes/health.py:13`). It touches
nothing. Readiness is `/readyz` (`api.yaml:78-82`, `services/readiness.py:38`), which
calls Chroma and Redis. With Chroma slow for 90 s, readiness fails 3 times at 10 s
intervals and the pods leave the Service endpoints: users get a fast 503 from the
Ingress. The containers keep running, so when Chroma recovers, the next probe adds
them back within 10 s. Nothing restarts.

**Swapped:** liveness would call Chroma every 20 s with `failureThreshold: 3`. After
about 60 s of slow Chroma, the kubelet kills **all three** api containers, because
they all depend on the same slow Chroma. Each restart then waits for its startup
probe while Chroma is still slow, so it fails liveness again and enters
CrashLoopBackOff with growing back-off (10 s, 20 s, 40 s ...). A 90-second slowdown
in one dependency becomes several minutes of total outage after Chroma has already
recovered. Readiness on `/healthz` would also keep routing traffic to pods that
cannot answer. The rule we follow: liveness protects the pod, readiness protects
the user.

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

`api.yaml:108` scales on `averageUtilization: 70` of `requests.cpu: 100m`
(`api.yaml:55`). While a request waits for Ollama, the api pod is blocked on a socket.
It uses almost no CPU, because the model runs on the laptop, not in the pod. So the
HPA measures JSON parsing, embedding HTTP calls and Prometheus scrapes. It shows
low CPU exactly when users are queueing for the LLM, and it will not scale up when
it matters. Our `kubectl top` under load is in `docs/evidence/kubectl-top.txt`.
With 8 questions sent at once (`scripts/load-test.sh`), 45 s in, while all of them
were in flight, the three api pods used **104m, 9m and 7m** CPU, and the HPA read
**`cpu: 8%/70%`**: no scale-up. Meanwhile the users waited **21, 54, 85, 111, 192,
219, 246 and 301 s**: Ollama answers one question at a time, so each one queued
behind the last. Four requests hit the 150 s LLM timeout, were retried once
(`llm reply rejected, retrying once` in the api log), and one ended in the safe
fallback answer. All 8 returned HTTP 200. The users were queueing at near-zero CPU,
which is exactly the case a CPU-based HPA cannot see. (The first reading in that
file, ~100m per pod, was taken less than a minute after the pods had restarted and is start-up
work, not idle load.)

The signal that should scale a generation service is **concurrency or queue depth**:
requests in flight per pod, or the p95 of `rahbar_request_seconds` and
`rahbar_generation_seconds` (both already exported at `/metrics`,
`routes/metrics.py`). To use it we would need:
1. a gauge `rahbar_inflight_requests`, incremented and decremented in the request
   middleware;
2. Prometheus scraping the pods;
3. prometheus-adapter (or KEDA) to expose it as a custom metric;
4. an HPA `type: Pods` metric with a target such as 2 in-flight requests per pod.

More api replicas only help while the LLM backend has spare capacity. With one
CPU-bound Ollama, the real bottleneck is the model server, which is why the semantic
cache matters.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

**Measured peak: 526 Mi.** We re-indexed with
`kubectl create job --from=cronjob/reindex` and sampled `kubectl top pods` every 15 s
(`docs/evidence/kubectl-top-indexer.txt`, 26 samples). The Job took 7 min 41 s for
690 chunks:

| Phase | Time | CPU | Memory |
| --- | --- | ---: | ---: |
| extract + OCR + clean + chunk | 15:40:20 – 15:42:24 | ~1250m | 338 → 392 Mi |
| embed (Ollama on the host does the work) and store | 15:42:24 – 15:47:55 | 3–765m | 512 → **526 Mi** |

Memory keeps rising during embedding because every chunk's 768-float vector is held
until the Chroma write at the end. metrics-server refreshes about once a minute, so a
spike shorter than that (one OCR page being rasterised) could be missed. That is one
more reason for the margin.

**Limit and margin.** `indexer-job.yaml` (and the same template in
`reindex-cronjob.yaml`) sets `requests.memory: 512Mi` and `limits.memory: 1536Mi`. The
margin is 1536 − 526 = **~1 GiB, about 2.9× the measured peak**. It covers a larger
corpus (more vectors held in memory), a heavier scanned page, and the sampling gap. The
request (512 Mi) sits at the steady peak, so the scheduler reserves what the Job really
uses.

**What happens when the limit is wrong** (`docs/evidence/indexer-oom.txt`): we ran
the same Job with the limit cut to 256 Mi. The kernel killed it 13 s after it started,
during extraction:

```
oom-demo-cq7g4   0/1   OOMKilled   0   18s
{"exitCode":137,"reason":"OOMKilled","startedAt":"2026-11-07T15:50:28Z","finishedAt":"2026-11-07T15:50:41Z"}
Warning  BackoffLimitExceeded  job-controller  Job has reached the specified backoff limit
```

**Exit code 137** = 128 + 9 (SIGKILL), with `reason: OOMKilled`. There is no Python
traceback and no log line, because the process gets no chance to write one. So
`kubectl describe pod` or the pod status is where you find it, not `kubectl logs`. With
the real `backoffLimit: 2` the Job would try three times, fail the same way each time,
and end `Failed`. The live index was untouched: the indexer writes to Chroma only
after all chunks are embedded, and the collection still showed
`index_version 20261107T154753Z` afterwards.

## 5. recall@5 by question type: where is it worst, and why?

Measured at the shipped setting (structure-aware 400 / 50; `eval/experiments/chunking.json`):

| Question type | recall@5 |
| --- | ---: |
| version (4) | 1.00 |
| injection (1 with relevant passages) | 1.00 |
| ordinary (11) | 0.86 |
| **exact term (5)** | **0.80** |

**Worst: exact-term questions.** The clearest miss is e01, "What does clause 1.21 of
the academic rules require?". Its top passage is a table-of-contents fragment
(`academic-rules-handbook-jun-2026.pdf :: ACADEMIC RULES AND`, similarity 0.69), and
the "Attendance Requirements" chunk that actually holds clause 1.21 is not in the top 5.

**Why embeddings struggle there:** a dense embedding compresses a passage into 768
numbers that describe what it is *about*. "1.21" carries almost no meaning on its own.
To `nomic-embed-text` it is close to "1.12", "1.22" or any other clause number, so the
question vector is dominated by "academic rules … require", and every chunk of the
handbook matches that a little. The same blur shows up in our cache measurements:
"…warning in **MS** programs?" and "…in **BS** programs?" score 0.9521, almost
identical, although one letter changes the answer (2.5 vs 2.0; docs/PIPELINE.md).
Version questions score best because they are worded like their passages ("withdraw
from a course" / "Withdrawal from a Course"), and the superseded filter removes the
near-duplicate old version.

**What addresses it (RAG lecture): hybrid retrieval.** Run a keyword index (BM25) next
to the vector search and merge the two rankings, e.g. with reciprocal rank fusion. BM25
matches the literal token "1.21" exactly where the embedding cannot. A cheaper partial
fix is to boost chunks whose text contains a clause number or code that appears in the
question. We did not ship either, so exact-term recall stays at 0.80 and is named
here as the next improvement.

## 6. EMBED_MODEL changed on the API only: what does your system do?

The indexer stamps `embed_model` and `dimension` into the collection metadata
(`indexer/pipeline/store.py:45-46`). At startup the api compares that with its own
`EMBED_MODEL` (`api/app/main.py:51`, which calls `services/readiness.py:79`
`pin_problem`). It repeats the comparison on every `/readyz`. On a mismatch, `/readyz`
returns 503 with the reason, so the pod stays `0/1 READY`, receives no traffic, and
logs one line naming both sides:

```
{"ts": "2026-11-07T15:27:42Z", "level": "ERROR", "logger": "rahbar.readiness", "msg": "not ready",
 "failed": {"embedding": "embedding model mismatch: index built with 'nomic-embed-text'
 (dimension 768), api configured with EMBED_MODEL='all-minilm'. Re-run the indexer or fix the ConfigMap."}}
```

We tested exactly this on the cluster: patched `EMBED_MODEL=all-minilm` into the
ConfigMap and restarted the api. The new pod stayed `0/1 Running`, the kubelet logged
`Readiness probe failed: HTTP probe failed with statuscode: 503` seven times in 55 s,
and the rollout stalled at "1 out of 3 new replicas have been updated" because
`maxUnavailable: 0` will not remove an old pod until a new one is Ready.

The other three replicas, still on the old ConfigMap until they restart, keep serving
correct answers. Without the guardrail, the pod would turn questions into vectors from
a different model, or vectors of a different length. With different lengths Chroma
errors. With the same length (two 768-dimension models) it silently returns the
"nearest" chunks of an unrelated vector space: plausible-looking, wrong passages,
cited confidently, with no error anywhere. Evidence: `docs/evidence/embedding-pin.txt`.

## 7. The failure that cost us more than an hour

TO BE WRITTEN BY THE TEAM: the symptoms, what you wrongly believed first, and the
exact command, metric or log line that finally told you the truth. Write it in your
own words, about a failure you lived through.

<!-- Reference only, delete when you write yours. One real candidate from this project,
     as the AI assistant recorded it while debugging extraction:

**Symptom.** The 2026 Academic Rules Handbook came out of extraction with every clause
twice: "1.13 All classes are held …" at line 259 and again at line 314 of the dump,
"5.4 Minimum CGPA …" twice, and so on. Words were also split ("attend ance",
"s emester", "2. 00"). Retrieval would have returned two near-identical chunks in its
top 5, halving the useful context, and "2.00" would never match "2. 00".

**What we wrongly believed first.** That it was a two-up print layout, two book pages
on one PDF page, and that each pair was being read twice. We tried cropping each page
into left and right halves (`page.crop((0, 0, w/2, h))`). Each half came out as
truncated words ("NATIONAL UNIVERSITY OF COMP" | "PUTER AND EMERGING SCIENCES"),
which proved the pages were *not* spreads: the halves cut through single lines. Then
we suspected a duplicated text layer (a hidden OCR layer under the real one) and
compared fonts per character. That was not it either: one font, `MazzardH-Medium`.

**What told us the truth.** A histogram of character x-positions on one page, from
pdfplumber:

```
x0 histogram [(-360, 36), (-320, 100), (-280, 184), ... (-40, 52), (40, 33), (80, 114), ... (360, 140)]
clause 1.9  -376 56     clause 1.15  43 130     clause 1.12 -340 200     clause 1.16  43 219
```

Half of the characters sit at **negative x**, outside the 420-point-wide page: the PDF
carries the *previous* page's text beside each page, invisible in a viewer but read
by every text extractor. The fix is one line in `indexer/pipeline/extract.py`,
`visible = page.within_bbox(page.bbox)`: keep only what is on the page. That fixed
the duplicates and the split words at once (both came from the overlapping text
runs). **Lesson:** when extracted text looks wrong, look at where the characters
are, not just what they say. The number that settled it was the sign of `x0`.
     -->
