#!/usr/bin/env bash
# Bring RAHBAR up on a kind cluster that already exists, then wait until it is ready.
# Used by both workflows (ci.yml e2e-eval and cd.yml deploy).
#
#   scripts/kind-e2e.sh <image-prefix> <tag>
#     scripts/kind-e2e.sh rahbar ci                       # images loaded with `kind load`
#     scripts/kind-e2e.sh docker.io/me/rahbar <sha>       # images pulled from Docker Hub
#
# Steps: Ollama for embeddings -> ingress controller -> apply the ci overlay ->
#        wait for the indexer Job -> wait for every rollout.
set -euo pipefail
PREFIX="${1:?image prefix}"
TAG="${2:?image tag}"
NS=rahbar
OVERLAY=k8s/overlays/ci

echo "== 1. Ollama on the kind network, for embeddings only (the LLM is the stub)"
docker run -d --name ollama --network kind ollama/ollama:0.6.8 >/dev/null
for _ in $(seq 1 30); do docker exec ollama ollama list >/dev/null 2>&1 && break; sleep 2; done
docker exec ollama ollama pull nomic-embed-text
OLLAMA_IP=$(docker inspect -f '{{.NetworkSettings.Networks.kind.IPAddress}}' ollama)
sed -i "s|OLLAMA_URL=.*|OLLAMA_URL=http://${OLLAMA_IP}:11434|" "$OVERLAY/ci.env"

echo "== 2. ingress-nginx for kind"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/kind/deploy.yaml
kubectl wait -n ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=180s

echo "== 3. apply the manifests (images ${PREFIX}-*:${TAG})"
(cd "$OVERLAY" && for app in api ui indexer; do
  kustomize edit set image "rahbar-${app}=${PREFIX}-${app}:${TAG}"
done)
kubectl apply -k "$OVERLAY"

echo "== 4. the indexer Job builds the index from the fixture corpus"
if ! kubectl wait -n "$NS" --for=condition=complete job/indexer --timeout=600s; then
  kubectl -n "$NS" describe job/indexer | tail -20
  kubectl -n "$NS" logs job/indexer --all-containers --tail=50 || true
  exit 1
fi
kubectl -n "$NS" logs job/indexer --tail=5

echo "== 5. wait for every rollout"
kubectl -n "$NS" rollout status statefulset/chroma --timeout=180s
kubectl -n "$NS" rollout status deployment/redis --timeout=180s
kubectl -n "$NS" rollout status deployment/api --timeout=300s
kubectl -n "$NS" rollout status deployment/ui --timeout=120s
kubectl -n "$NS" get pods -o wide
