# ADR 0001: The indexer is a Kubernetes Job, and the api only reads

## Status
Accepted · 4 November 2026

## Context
RAHBAR does two very different kinds of work. **Indexing** (download, extract, OCR,
clean, chunk, embed, store) runs rarely, takes minutes, and needs lots of memory: OCR
of the two scanned PDFs and embedding about 690 chunks. **Querying** runs on every
question, must answer in seconds, and needs little memory. The api runs as 3 replicas
(HPA 3 to 8), and there is exactly one Chroma volume (`chroma-data-chroma-0`).

The obvious design is "index on api startup". With 3 replicas that breaks in four ways:

1. **Three writers on one volume.** Three pods start together, and each one deletes and
   rebuilds the same collection while the others are halfway through theirs. The
   result is an index that no single run produced: chunks missing, duplicated, or
   from a run that later failed.
2. **Every restart pays the full cost again.** A rolling update, an OOMKill, a node
   drain or a probe blip would each mean minutes of OCR and embedding, three times over.
3. **Readiness becomes a lie.** Either the pod says "ready" before the index exists and
   serves empty retrievals, or it stays unready for minutes and the startupProbe
   (30 × 2 s = 60 s) kills it mid-index, so a rollout never finishes.
4. **No re-index without a redeploy.** New documents would mean restarting the api.

## Decision
- Indexing lives in its own image (`indexer/`) and runs as the **Job `indexer`**
  (`k8s/base/indexer-job.yaml`), with `restartPolicy: OnFailure` and `backoffLimit: 2`.
  The **CronJob `reindex`** (`k8s/base/reindex-cronjob.yaml`) has the same pod template
  and runs weekly with `concurrencyPolicy: Forbid`, so two indexers never overlap.
- Re-indexing is one command, with no redeploy and no downtime:
  `kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar`
- The indexer is the **only writer**. The api code never calls `add`, `upsert` or
  `delete` (`api/app/retrieval/chroma.py` only calls `query`, `count` and
  `get_collection`). The NetworkPolicy allows the api and the indexer to reach Chroma;
  only the indexer's image holds the writing code.
- The api's **readiness probe** (`/readyz`, `api/app/services/readiness.py`) returns 503
  until Chroma is reachable, the collection is **non-empty**, its recorded embedding
  model equals the api's `EMBED_MODEL`, and Redis answers. So a pod is kept out of the
  Service until there is something correct to retrieve.
- **What happens to the old index while the api serves from it** (`indexer/pipeline/store.py`):
  - Same embedding model: the Job updates the collection in place. It upserts every
    new chunk, deletes chunks that no longer exist, and only then writes a new
    `index_version`. The api answers from a complete index the whole time.
  - Different embedding model: old vectors are useless, so the collection is rebuilt.
    The api reports not-ready until it is filled. That is a brief, honest outage
    instead of wrong answers.

## Options considered
- **Index in an initContainer of the api.** Same race as indexing on startup: each of
  the 3 replicas gets its own initContainer.
- **A leader-elected api pod that indexes.** That needs RBAC for leases and puts a
  minutes-long, memory-heavy task inside a latency-sensitive server. Too much
  machinery to avoid one Job.
- **Index at image build time (bake the vectors into the image).** That couples the
  index to a rebuild, which is the "configuration change that needs a rebuild"
  problem from Lecture 05.

## Consequences
- Easier: the api stays small (requests 160Mi) and starts in seconds. The indexer gets
  its own, larger memory limit (1536Mi), sized from `kubectl top`. Re-indexing is a
  command, not a deployment.
- Harder: there is an ordering dependency. On a fresh cluster the api pods sit at 0/1
  READY until the Job finishes. That is intended, and the README apply order says so.
- To watch: the Job and the CronJob share a pod template by copy. If one is changed,
  the other must be changed too (a comment in both files says so).
