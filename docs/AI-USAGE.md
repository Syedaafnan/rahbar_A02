# AI usage

We used one AI tool: **Claude Code** (Anthropic, model Claude Opus 5.5), inside VS Code,
from 3 November 2026. It ran commands on our machine and wrote files; we committed
everything ourselves.

| Tool | What it wrote or shaped | What we changed afterwards, and why |
| --- | --- | --- |
| Claude Code | **Plan and schedule** for the week, from the assignment PDF. | We set the rules: keep everything simple and beginner-friendly, and we commit everything ourselves under our own names. |
| Claude Code | **Corpus investigation.** Ran `fetch.py`, counted pages and text layers with pypdf, found that the 2023 rules are a scan (24/26 pages), confirmed the start-up policy with `pdffonts`, OCR'd both scans in the indexer image, and found the four clauses where 2023 and 2026 disagree. Drafted `docs/CORPUS.md`. | |
| Claude Code | **Golden set** (`eval/golden.yaml`), written from the documents before any pipeline code existed. | |
| Claude Code | **Dockerfiles** and all **Kubernetes manifests** (base plus dev/prod/ci overlays). | We asked for simpler manifests. It then removed the Pod Security Admission labels and the PodDisruptionBudget (not required, and a source of confusing errors), and shortened the corpus-fetch initContainer to one command. |
| Claude Code | **Indexer pipeline** (`indexer/pipeline/*.py`). It diagnosed the "every clause appears twice" problem: the 2026 handbook stores the neighbouring page's text at negative x coordinates. It added the off-page filter and gutter-based column splitting for the prospectus. Its own tests caught one of its bugs (the header remover also deleted clauses that differ only by their numbers), which it fixed. | |
| Claude Code | **Query pipeline** (`api/app/**`): Chroma retriever, prompt, Ollama and hosted providers, grounding rules, retry/fallback, Redis cache, readiness and the embedding pin, metrics. | |
| Claude Code | **Tests** (`api/tests/test_pipeline.py`, `indexer/tests/test_pipeline.py`) and the four **evaluation** functions in `eval/run_eval.py`. | |
| Claude Code | **CI/CD workflows** and `scripts/kind-e2e.sh`. | |
| Claude Code | First drafts of **ADRs 0001 to 0004**, `ARCHITECTURE.md`, the README, and this file. | |


