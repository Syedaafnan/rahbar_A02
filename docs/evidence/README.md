# Evidence

Command output captured from the minikube cluster (`--cni=calico`, Kubernetes 1.37) on
7 November 2026, plus GitHub screenshots. Each file starts with the command that made it.

| File | Shows | Section |
| --- | --- | --- |
| `kubectl-get-all.txt` | every object in `rahbar`: pods, services, PVCs, Job, CronJob, HPA, Ingress | 4.1 |
| `kubectl-top.txt` | `kubectl top pods` before and under load (`scripts/load-test.sh`, 8 concurrent questions) | 4.5, notes Q3 |
| `kubectl-top-indexer.txt` | `kubectl top pods` every 15 s while a re-index Job runs: the indexer's peak memory | 4.5, notes Q4 |
| `indexer-oom.txt` | the same Job with its memory limit cut to 256 Mi: `OOMKilled`, exit code 137, live index untouched | notes Q4 |
| `hpa.txt` | `kubectl get hpa` with a real CPU percentage (not `<unknown>`) | 4.5 |
| `endpoints.txt` | 3 api pod IPs behind the Service, found by label; then scaled to 4 | 4.6 |
| `rollout.txt` | `kubectl set image` → `rollout status` → `rollout undo`, old version serving again | 4.6 |
| `embedding-pin.txt` | EMBED_MODEL changed on the api only: pod `0/1 READY` and the log line naming the conflict | 4.4, notes Q6 |
| `networkpolicy.txt` | the ui pod cannot reach chroma; the api pod can | 4.7 |
| `delete-pod.txt` | an api pod and `chroma-0` deleted; answers keep coming, the index survives | 1.3 |
| `reindex.txt` | `kubectl create job --from=cronjob/reindex`, cache invalidated by the new index_version | 3.3, 4.2 |
| `ask-examples.txt` | a cited answer, an abstention (hostel), an old-version question, through the Ingress | 1.3 |
| `branch-protection.png` | GitHub rule for `main`: pull request required, 1 approval required (the 8 CI jobs are required checks too) | 6 |
| `branch-protection-rule.png` | the rule list: pattern `main`, applies to 1 branch | 6 |
| `blocked-merge.png` | a PR failing the eval gate (`RELEVANCE_FLOOR=0.3` in the CI overlay), merging blocked | 6, 5.4 |
| `gate-failure.txt` | the e2e-eval log of that failing run: `GATE FAILED`, abstention correctness 0.0 (f05 answered) | 5.4 |
| `green-merge.png` | a PR with all 16 checks passed (8 jobs × push and pull_request) | 6 |
| `cd-run.png` | `cd.yml` on main: test → build-push (SHA-tagged images on Docker Hub) → deploy, all green | 6 |
| `ui-screenshot.png` | the chat UI answering "2" from the handbook, with its source (used in the README) | 10 |
