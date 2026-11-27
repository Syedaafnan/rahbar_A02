# k8s/: from a fresh cluster to a working RAGOps pipeline

Every object RAHBAR needs is in this folder. A fresh minikube reaches a working,
evaluated pipeline with the commands below, in this order. Run them from the repository
root in **Git Bash** (Windows) or any POSIX shell.

## 0. Prerequisites (once)

```bash
# Ollama on the laptop, listening on all interfaces (Windows: setx OLLAMA_HOST 0.0.0.0, restart Ollama)
ollama pull nomic-embed-text      # live embedding model
ollama pull mxbai-embed-large     # candidate embedding model (Part C)
ollama pull all-minilm            # deliberately bad candidate (Part C)
ollama pull llama3.2:3b           # answers and the faithfulness judge

minikube start --driver=docker --cni=calico --memory=6144 --cpus=4
minikube addons enable metrics-server
minikube addons enable ingress
minikube ssh "grep host.minikube.internal /etc/hosts"   # must be 192.168.65.254, else edit overlays/dev
# Pods must resolve host.minikube.internal. minikube usually adds it to CoreDNS; on our
# laptop (minikube with Calico) it did not, and the indexer failed with "ollama down".
# Check, and if the hosts block is missing, add it:
kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' | grep -q host.minikube.internal || \
  python scripts/fix_coredns.py
make images                       # builds rahbar-api, -indexer, -ui, -eval :dev and loads them
```

## 1. The live pipeline (F1, F2)

```bash
kubectl apply -k k8s/overlays/dev             # namespace, ConfigMaps, PVCs, policies, chroma,
                                              # redis, api (3 replicas), ui, ingress, indexer Job,
                                              # reindex CronJob
kubectl -n rahbar wait --for=condition=complete job/indexer --timeout=30m
kubectl -n rahbar rollout status deployment/api
```

| Object | File | RAGOps responsibility |
| --- | --- | --- |
| Deployment `api` (+ HPA, Service, Ingress) | base/api.yaml, base/services.yaml | the query pipeline; reads Chroma, never writes it; one trace line per query to stdout and `/traces` |
| StatefulSet `chroma` + its PVC | base/chroma-statefulset.yaml | the vector store; survives pod deletion |
| PVC `corpus-data` | base/indexer-job.yaml | the data lake: `lake/raw/<batch>`, `lake/quarantine`, `lake/archive`, `state/` (registry), `reports/` |
| PVC `traces`, PVC `eval-results` | base/pvcs.yaml | trace files for the report scripts; evaluation result files and gate decisions |
| Job `indexer`, CronJob `reindex` | base/indexer-job.yaml, base/reindex-cronjob.yaml | ingest → verify → update lake → update retrieval sources; **the only writer to Chroma**: the only code that calls upsert/delete is `indexer/pipeline/store.py`, which only these pods run; `concurrencyPolicy: Forbid` stops two writers overlapping; the api code only queries |
| ConfigMap `rahbar-config` | base/configmap.yaml | the live release: embedding model (the pin), prefixes, collection, thresholds |
| ConfigMap `rahbar-gate` | base/gate-configmap.yaml | release gate and coverage thresholds, mounted at `/config/gate.yaml` |
| NetworkPolicies | base/networkpolicy.yaml | default deny; only listed pods reach Chroma, Redis, Ollama |

## 2. Batches and incremental updates (Part A)

```bash
python scripts/lake.py push corpus/batch_b    # copies Batch B into /data/lake/raw/batch_b
python scripts/run_indexer.py                 # Job from cronjob/reindex: verification report + counts
python scripts/run_indexer.py                 # again, unchanged corpus: EMBEDDED 0
python scripts/run_indexer.py --count-by-doc  # chunks per doc_id, flags orphans
```

## 3. Evaluation and release gate (Part B, C, F2, F3)

```bash
kubectl apply -f k8s/eval-baseline-job.yaml   # baseline result -> /results/baseline.json
kubectl apply -f k8s/eval-job.yaml            # the suite against the live api, gate applied

kubectl apply -k k8s/candidate                # api-candidate Deployment + Service, candidate ConfigMaps
kubectl apply -f k8s/candidate/indexer-candidate-job.yaml   # builds collection rahbar-mxbai
kubectl apply -f k8s/candidate/indexer-bad-job.yaml         # builds collection rahbar-bad

python scripts/release.py stage bad && python scripts/release.py evaluate bad    # gate FAILS
python scripts/release.py promote bad                                            # REFUSED
python scripts/release.py stage good && python scripts/release.py evaluate good  # gate passes
python scripts/release.py promote good        # rolling update, kubectl rollout status
python scripts/release.py undo                # kubectl rollout undo deployment/api
python scripts/release.py ask "What CGPA puts a student on academic warning?"   # trace: collection + model
```

Gate thresholds without a rebuild (F2):

```bash
kubectl apply -f k8s/gate-job.yaml && kubectl -n rahbar logs -f job/gate          # PASS
kubectl -n rahbar patch configmap rahbar-gate --type merge --patch-file k8s/gate-strict.patch.json
kubectl -n rahbar delete job gate && kubectl apply -f k8s/gate-job.yaml && kubectl -n rahbar logs -f job/gate   # FAIL
kubectl apply -k k8s/overlays/dev             # restores the normal thresholds
```

## 4. Coverage (Part E)

```bash
kubectl -n rahbar delete job coverage --ignore-not-found && kubectl apply -f k8s/coverage-job.yaml
```

## 5. Observability (F4)

```bash
kubectl -n rahbar logs deploy/api | grep '"type": "trace"' | tail -1
kubectl -n rahbar port-forward svc/api 18000:8000 &   curl -s localhost:18000/metrics | grep rahbar_
python scripts/lake.py pull                   # results, traces, ingest reports -> repo
```

## Notes

- A Job's pod template is immutable: delete a finished Job before re-applying it.
- `kubectl apply -k k8s/overlays/dev` after a release resets the api to `rahbar-config`
  (that is the declared state); use `scripts/release.py status` to see what is live.
- Resource requests and limits come from `kubectl top` measurements, recorded in
  `eval/results/resources.txt` and the report.
