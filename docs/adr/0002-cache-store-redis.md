# ADR 0002: The semantic cache lives in Redis, keyed by index version

## Status
Accepted · 4 November 2026

## Context
A CPU-only `llama3.2:3b` takes tens of seconds per answer, and students ask the same
questions in different words ("withdrawal deadline?" / "last date to withdraw from a
course?"). A semantic cache returns a stored answer when a new question's embedding is
close enough to a stored one. Section 3.3 adds three rules: the threshold comes from the
ConfigMap, unsafe answers are never cached, and a re-index must invalidate the cache.

## Decision
- **Redis** (`k8s/base/redis.yaml`: a Deployment with `strategy: Recreate`, a PVC
  `cache-data`, append-only persistence, `maxmemory 96mb` with `allkeys-lru`).
- One key per entry: `rahbar:cache:<index_version>:<sha1(question)>`, holding JSON with
  the question, its embedding and the full response. TTL from `CACHE_TTL_SECONDS`.
- **Lookup** (`api/app/services/cache.py`) scans the keys of the current index_version
  and computes cosine similarity in Python. A hit needs similarity ≥ `CACHE_THRESHOLD`.
  The question is embedded once per request; the same vector serves the cache and the
  retriever.
- **Invalidation without a network path:** the NetworkPolicy lets only the api reach
  Redis, so the indexer cannot flush it. Instead the indexer stamps a new
  `index_version` into the Chroma collection metadata on every run, and the api builds
  its keys from it. After a re-index the old keys are simply never looked at again,
  and they expire by TTL.
- **Never cached** (`api/app/services/answer.py`, step 8): follow-up questions,
  abstentions, fallbacks, and answers whose citations failed the grounding check.
- **Contract:** no schema change. Cache status goes back in an `X-Cache: hit|miss|skip`
  header, which the starter UI already shows as a chip. `Passage` gained two internal
  fields (`doc_version`, `superseded`) so the prompt can label old versions; they are
  not part of `AskResponse`.

## Options considered
- **A second Chroma collection:** that would give us vector search for free. But it
  puts a write path from the api into the vector store that ADR 0001 says only the
  indexer writes to, and it would need "delete all" on every re-index.
- **Redis Stack vector search:** fast for millions of entries, but a different image,
  and a cache in a course project holds hundreds of entries. A linear scan of a few
  hundred 768-dimension vectors takes milliseconds.
- **In-process dict per api pod:** no extra Deployment, but three replicas would have
  three different caches, all of them lost on every restart.

## Consequences
- Easier: one well-known image, persistence across restarts, and invalidation that
  needs no extra network rule.
- Harder: the lookup is O(entries). If the cache ever held tens of thousands of
  entries, we would move to Redis vector search or an approximate index.
- To watch: `rahbar_cache_hit_ratio` and the cache-hit log line with its `similarity`.
  A false hit (a different question answered from cache) means the threshold is too
  low; see docs/PIPELINE.md.
