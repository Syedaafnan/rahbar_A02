#!/usr/bin/env bash
# Send N concurrent questions through the Ingress, so `kubectl top pods` and
# `kubectl get hpa` show the api under load (docs/evidence, ENGINEERING-NOTES Q3).
#
#   scripts/load-test.sh 127.0.0.1 8          # host, concurrency
set -uo pipefail
HOST_IP="${1:-127.0.0.1}"
CONCURRENCY="${2:-8}"
QUESTIONS=(
  "What is the last date to withdraw from a course?"
  "What CGPA does an undergraduate need to stay off academic warning?"
  "How many courses can a student repeat in the summer semester?"
  "What is the tuition fee per credit hour for BS programs?"
  "How much of my fee is refunded if I leave on the 12th day?"
  "What attendance is required in lectures?"
  "Who has to approve a course withdrawal?"
  "What are unacceptable uses of generative AI tools?"
)
for i in $(seq 1 "$CONCURRENCY"); do
  q="${QUESTIONS[$(( (i - 1) % ${#QUESTIONS[@]} ))]} (load $i)"
  curl -s -o /dev/null -w "request $i: %{http_code} in %{time_total}s\n" \
    -H "Host: rahbar.local" -H "Content-Type: application/json" \
    -d "{\"question\": \"$q\"}" "http://$HOST_IP/api/ask" &
done
wait
