#!/usr/bin/env bash
# Capture the cluster evidence for docs/evidence/ (sections 4.4-4.7, 1.3).
# Run from the repository root in Git Bash, with the stack deployed and Ready, and the
# Ingress reachable (in another terminal):
#   kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 18080:80
#
#   bash scripts/capture-evidence.sh            # everything (about 20 minutes)
#   bash scripts/capture-evidence.sh pin        # one section: all|basics|ask|load|endpoints|
#                                               #   netpol|pin|rollout|delete|reindex|oom
set -uo pipefail
NS=rahbar
INGRESS="${INGRESS:-http://127.0.0.1:18080}"
OUT=docs/evidence
ONLY="${1:-all}"
mkdir -p "$OUT"

# run FILE COMMAND... : append "$ command" and its output to docs/evidence/FILE
run() { local f="$OUT/$1"; shift; { echo; echo "\$ $*"; eval "$@" 2>&1; } | tee -a "$f"; }
start() { echo "# $2" > "$OUT/$1"; echo "# captured $(date -u +%Y-%m-%dT%H:%M:%SZ) on minikube $(kubectl version 2>/dev/null | grep Server | cut -d: -f2)" >> "$OUT/$1"; }
want() { [ "$ONLY" = all ] || [ "$ONLY" = "$1" ]; }
ask() {  # ask "question" [request-id]
  curl -s -i --max-time 400 -H "Host: rahbar.local" -H "Content-Type: application/json"     -H "X-Request-ID: ${2:-evidence-$RANDOM}" -d "{\"question\": \"$1\"}" "$INGRESS/api/ask"     | python scripts/show_answer.py
}
api_pod() { kubectl -n $NS get pods -l app.kubernetes.io/name=api --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'; }
collection_meta() {  # Chroma collection metadata, read from inside an api pod (read-only)
  kubectl -n $NS exec "$(api_pod)" -- python -c "import json, urllib.request as u; c=json.load(u.urlopen('http://chroma:8000/api/v2/tenants/default_tenant/databases/default_database/collections/rahbar')); print(json.dumps(c['metadata']))"
}

if want basics; then
  start kubectl-get-all.txt "Every object in the rahbar namespace (section 4.1)"
  run kubectl-get-all.txt kubectl -n $NS get all,pvc,ingress,networkpolicy,serviceaccount,configmap -o wide
  start hpa.txt "HPA reading a real CPU percentage, not <unknown> (section 4.5)"
  run hpa.txt kubectl -n $NS get hpa api
  run hpa.txt kubectl -n $NS describe hpa api
fi

if want ask; then
  start ask-examples.txt "Questions through the Ingress: cited answer, abstention, old version (section 1.3)"
  run ask-examples.txt ask "'What percentage of the fee is refunded up to the 15th day of classes?'" demo-cited
  run ask-examples.txt ask "'What is the monthly hostel fee at the Peshawar campus?'" demo-abstain
  run ask-examples.txt ask "'Under the 2023 academic rules, what was the deadline to withdraw from a course?'" demo-old-version
  run ask-examples.txt ask "'What percentage of the fee is refunded up to the 15th day of classes?'" demo-cache-repeat
fi

if want load; then
  start kubectl-top.txt "kubectl top pods: idle, then under 8 concurrent questions (section 4.5, notes Q3)"
  run kubectl-top.txt kubectl -n $NS top pods
  bash scripts/load-test.sh 127.0.0.1:18080 8 > /tmp/rahbar-load.txt 2>&1 &
  LOAD=$!
  sleep 45
  run kubectl-top.txt "echo '--- under load (requests in flight, waiting on the LLM)'; kubectl -n $NS top pods"
  run kubectl-top.txt kubectl -n $NS get hpa api
  wait $LOAD
  run kubectl-top.txt cat /tmp/rahbar-load.txt
fi

if want endpoints; then
  start endpoints.txt "The api Service finds its pods by label: 3 IPs, then 4 after scaling (section 4.6)"
  # The HPA owns the replica count (min 3), so a plain `kubectl scale` is undone within
  # seconds. Raise the HPA minimum to 4 instead, then put it back.
  EPS="-o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}  ready={.conditions.ready}  pod={.targetRef.name}{\"\\n\"}{end}'"
  run endpoints.txt kubectl -n $NS get endpointslices -l kubernetes.io/service-name=api
  run endpoints.txt kubectl -n $NS get endpointslices -l kubernetes.io/service-name=api "$EPS"
  run endpoints.txt "kubectl -n $NS patch hpa api --type merge -p '{\"spec\":{\"minReplicas\":4}}'"
  sleep 20
  run endpoints.txt kubectl -n $NS rollout status deployment/api --timeout=180s
  run endpoints.txt kubectl -n $NS get endpointslices -l kubernetes.io/service-name=api "$EPS"
  run endpoints.txt kubectl -n $NS get pods -l app.kubernetes.io/name=api -o wide
  run endpoints.txt "kubectl -n $NS patch hpa api --type merge -p '{\"spec\":{\"minReplicas\":3}}'"
  run endpoints.txt "kubectl -n $NS get endpoints api 2>/dev/null   # the command named in section 4.6 (deprecated in favour of EndpointSlices since 1.33, same data)"
fi

if want netpol; then
  start networkpolicy.txt "NetworkPolicy (Calico): the ui pod cannot reach chroma, the api pod can (section 4.7)"
  UI=$(kubectl -n $NS get pods -l app.kubernetes.io/name=ui -o jsonpath='{.items[0].metadata.name}')
  run networkpolicy.txt kubectl -n $NS get networkpolicy
  run networkpolicy.txt "kubectl -n $NS exec $UI -- wget -q -T 5 -O- http://chroma:8000/api/v2/heartbeat; echo \"exit code: \$? (non-zero = blocked)\""
  run networkpolicy.txt "kubectl -n $NS exec $UI -- wget -q -T 5 -O- http://redis:6379; echo \"exit code: \$? (non-zero = blocked)\""
  run networkpolicy.txt "kubectl -n $NS exec $(api_pod) -- python -c \"import urllib.request as u; print('api -> chroma:', u.urlopen('http://chroma:8000/api/v2/heartbeat', timeout=5).read().decode())\""
fi

if want pin; then
  start embedding-pin.txt "Embedding pin (section 4.4): EMBED_MODEL changed on the api only, then restored"
  run embedding-pin.txt collection_meta
  run embedding-pin.txt "kubectl -n $NS patch configmap rahbar-config --type merge -p '{\"data\":{\"EMBED_MODEL\":\"all-minilm\"}}'"
  run embedding-pin.txt kubectl -n $NS rollout restart deployment/api
  sleep 60
  run embedding-pin.txt kubectl -n $NS get pods -l app.kubernetes.io/name=api
  NEW=$(kubectl -n $NS get pods -l app.kubernetes.io/name=api --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
  run embedding-pin.txt "kubectl -n $NS logs $NEW | grep -m1 'not ready'"
  run embedding-pin.txt "kubectl -n $NS describe pod $NEW | grep -E 'Readiness probe failed|Ready:' | head -3"
  run embedding-pin.txt "echo '--- old pods keep serving meanwhile (maxUnavailable: 0):'; kubectl -n $NS rollout status deployment/api --timeout=5s || true"
  run embedding-pin.txt "kubectl -n $NS patch configmap rahbar-config --type merge -p '{\"data\":{\"EMBED_MODEL\":\"nomic-embed-text\"}}'"
  run embedding-pin.txt kubectl -n $NS rollout restart deployment/api
  run embedding-pin.txt kubectl -n $NS rollout status deployment/api --timeout=300s
fi

if want rollout; then
  start rollout.txt "Rollout and rollback (section 4.6): set image -> rollout status -> rollout undo"
  run rollout.txt "kubectl -n $NS get deploy api -o jsonpath='{.spec.template.spec.containers[0].image}'; echo"
  run rollout.txt kubectl -n $NS set image deployment/api api=rahbar-api:v2
  run rollout.txt kubectl -n $NS rollout status deployment/api --timeout=300s
  run rollout.txt "kubectl -n $NS get deploy api -o jsonpath='{.spec.template.spec.containers[0].image}'; echo"
  run rollout.txt "kubectl -n $NS logs deploy/api | grep -m1 'api started'"
  run rollout.txt kubectl -n $NS rollout history deployment/api
  run rollout.txt kubectl -n $NS rollout undo deployment/api
  run rollout.txt kubectl -n $NS rollout status deployment/api --timeout=300s
  run rollout.txt "kubectl -n $NS get deploy api -o jsonpath='{.spec.template.spec.containers[0].image}'; echo"
  run rollout.txt ask "'What attendance is required in lectures?'" after-undo
fi

if want delete; then
  start delete-pod.txt "Delete an api pod and chroma-0: nothing breaks, the index survives (section 1.3)"
  run delete-pod.txt kubectl -n $NS delete pod "$(api_pod)" --wait=false
  run delete-pod.txt kubectl -n $NS get pods -l app.kubernetes.io/name=api
  run delete-pod.txt ask "'How many courses can a student repeat in the summer semester?'" during-api-delete
  run delete-pod.txt collection_meta
  run delete-pod.txt kubectl -n $NS delete pod chroma-0
  run delete-pod.txt kubectl -n $NS rollout status statefulset/chroma --timeout=180s
  run delete-pod.txt kubectl -n $NS get pvc
  run delete-pod.txt "echo 'collection after chroma-0 restarted (same PVC, same index):'"
  run delete-pod.txt collection_meta
  run delete-pod.txt "kubectl -n $NS rollout status deployment/api --timeout=180s"
fi

if want reindex; then
  start reindex.txt "Re-index without a redeploy; cache invalidated by the new index_version (sections 3.3, 4.2)"
  run reindex.txt collection_meta
  run reindex.txt ask "'What is the tuition fee per credit hour for BS programs in 2026-27?'" before-reindex-1
  run reindex.txt ask "'What is the tuition fee per credit hour for BS programs in 2026-27?'" before-reindex-2
  JOB=manual-reindex-$(date +%s)
  run reindex.txt kubectl create job --from=cronjob/reindex $JOB -n $NS
  start kubectl-top-indexer.txt "kubectl top pods sampled every 15 s while the indexer Job runs (notes Q4)"
  while ! kubectl -n $NS wait --for=condition=complete job/$JOB --timeout=1s >/dev/null 2>&1; do
    kubectl -n $NS top pods -l job-name=$JOB --no-headers 2>/dev/null | tee -a "$OUT/kubectl-top-indexer.txt"
    kubectl -n $NS get job $JOB -o jsonpath='{.status.failed}' | grep -q . && break
    sleep 15
  done
  run reindex.txt kubectl -n $NS get job $JOB
  run reindex.txt "kubectl -n $NS logs job/$JOB -c indexer | tail -3"
  run reindex.txt collection_meta
  run reindex.txt ask "'What is the tuition fee per credit hour for BS programs in 2026-27?'" after-reindex
  run reindex.txt "kubectl -n $NS get pod -l job-name=$JOB -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated}'; echo"
fi

if want oom; then
  # Same Job as the CronJob, but with the memory limit cut to 256Mi (the measured
  # peak is ~526Mi) and no retries. It is killed during extraction, before it writes
  # anything to Chroma, so the live index is untouched. restartPolicy Never keeps the
  # failed pod, so its exit code can be read (with OnFailure the Job deletes it).
  start indexer-oom.txt "The indexer with a memory limit below its peak: OOMKilled, exit code 137 (notes Q4)"
  run indexer-oom.txt "kubectl create job --from=cronjob/reindex oom-demo -n $NS --dry-run=client -o yaml | sed -e 's/memory: 1536Mi/memory: 256Mi/' -e 's/memory: 512Mi/memory: 256Mi/' -e 's/backoffLimit: 2/backoffLimit: 0/' -e 's/restartPolicy: OnFailure/restartPolicy: Never/' | kubectl apply -f -"
  run indexer-oom.txt "kubectl -n $NS get job oom-demo -o jsonpath='{.spec.template.spec.containers[0].resources}'; echo"
  kubectl -n $NS wait --for=condition=failed job/oom-demo --timeout=600s >/dev/null 2>&1
  run indexer-oom.txt kubectl -n $NS get job oom-demo
  run indexer-oom.txt "kubectl -n $NS get pods -l job-name=oom-demo"
  run indexer-oom.txt "kubectl -n $NS get pod -l job-name=oom-demo -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated}'; echo"
  run indexer-oom.txt "kubectl -n $NS describe job oom-demo | tail -4"
  run indexer-oom.txt "echo 'live index untouched:'"
  run indexer-oom.txt collection_meta
  run indexer-oom.txt kubectl -n $NS delete job oom-demo
fi

echo
echo "Evidence written to $OUT/. Commit these .txt files."
