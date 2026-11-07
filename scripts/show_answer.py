"""Pretty-print an /api/ask response read from stdin (`curl -i` output).
Used by scripts/capture-evidence.sh; prints the status line, the X-Cache and
X-Request-ID headers, the answer fields and what was retrieved."""

import json
import sys

for raw in sys.stdin:
    line = raw.rstrip()
    if not line.startswith("{"):
        if line.upper().startswith(("HTTP/", "X-CACHE", "X-REQUEST-ID")):
            print(line)
        continue
    r = json.loads(line)
    shown = {k: r.get(k) for k in ("request_id", "answer", "citations",
                                    "answered_from_context", "confidence")}
    print(json.dumps(shown, indent=1, ensure_ascii=False))
    print("retrieved:")
    for p in r.get("retrieved", []):
        print(f"  {p['score']:.4f}  {p['source']} :: {p['section']}")
