# ADR 0004: Pluggable LLM provider; local by default, and what leaves the cluster

## Status
Accepted · 4 November 2026

## Context
Students may type personal details into a question ("my roll number is 23I-xxxx and my
CGPA is 1.8, will my admission be closed?"). Whatever the api sends to an LLM leaves our
control if that LLM is hosted. We also need CI to run with no model at all, and the
laptop has no GPU.

## Decision
- One interface, `LLMProvider.generate(question, passages) -> Answer`
  (`api/app/providers/llm/base.py`), chosen by `LLM_PROVIDER` in the ConfigMap:
  - `ollama-host` **(default):** Ollama on the laptop at host.minikube.internal:11434,
    with `llama3.2:3b`. Questions and passages never leave the machine.
  - `hosted`: any OpenAI-compatible endpoint (Groq by default), JSON mode on. Opt-in.
    The key comes only from the `rahbar-secret` Secret, created on the command line.
  - `stub`: deterministic, no network. Used by every unit test and by the CI/CD clusters.
  All three share one prompt builder (`prompt.py`), so swapping a provider is one file.
- **Embeddings always stay local** (`nomic-embed-text` on Ollama), also in hosted mode.
  The index is built from public documents, but questions are embedded too.
- **Turns are stored in the cluster only:** step 8 of the query pipeline records each
  turn (question, answer, citations, outcome) in the Redis list `rahbar:turns`, capped
  at the newest 1000 (`services/cache.py`, `record_turn`). Redis is ClusterIP and the
  NetworkPolicy lets only the api reach it, so the record never leaves the cluster.
  It exists so wrong answers can be found and added to the golden set.
- **Logs carry no question text and no answer text:** only the request id, chunk ids,
  scores, flags and timings (`services/answer.py`, `_log`). The model's `reasoning` is
  never returned to the user.
- **What leaves the cluster in hosted mode:** the system prompt, the retrieved public
  passages, and the user's question. Nothing else (no ids, no logs). The README states
  this next to the hosted setup.

## Options considered
- **Hosted by default:** much faster answers, but every student question would go to a
  third party by default. We chose fast as opt-in and private as the default.
- **In-cluster Ollama** (`ollama-incluster`): the same privacy as ollama-host, but the
  model needs about 3 GB inside a 5 GB minikube. The provider supports it (same
  class), but we do not ship that Deployment.

## Consequences
- Default answers are slow on CPU (tens of seconds), so the Ingress read timeout is
  raised to 120 s and the cache matters (ADR 0002).
- Switching to hosted is a ConfigMap change, a `kubectl create secret`, and a restart.
  No rebuild is needed.
- If hosted mode is used with real students, the privacy notice must say that
  questions go to the provider. We would also add a PII scrub (roll numbers, phone
  numbers) before the call.
