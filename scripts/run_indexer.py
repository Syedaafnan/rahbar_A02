#!/usr/bin/env python3
"""Run the ingestion Job once (A03 Part A, F2) and print its logs.

The Job is created from the CronJob's own template (`cronjob/reindex`), so an
on-demand run and the nightly run are the same pod spec: same image, same ConfigMap,
same data lake PVC, and the same single writer to the vector store.

    python scripts/run_indexer.py                       # ingest: verify + incremental update
    python scripts/run_indexer.py --count-by-doc        # chunks per doc_id, flags orphans (A2)
    python scripts/run_indexer.py --restore DOC_ID      # rollback a family to an archived version
    python scripts/run_indexer.py --unpin FAMILY        # undo the restore
    python scripts/run_indexer.py --config rahbar-config-candidate   # another collection
    python scripts/run_indexer.py --save eval/results/A2-unchanged.txt   # keep the log

Exit code: the Job's (0 = Complete, 1 = Failed or timed out).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

NS = "rahbar"


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    out = subprocess.run(["kubectl", "-n", NS, *args], input=stdin, text=True,
                         capture_output=True, encoding="utf-8")
    if check and out.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed:\n{out.stderr}")
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the RAHBAR ingestion Job once.")
    ap.add_argument("--count-by-doc", action="store_true")
    ap.add_argument("--restore", metavar="DOC_ID")
    ap.add_argument("--unpin", metavar="FAMILY")
    ap.add_argument("--config", default="rahbar-config",
                    help="ConfigMap to read (the collection and model come from it)")
    ap.add_argument("--name", help="Job name (default: ingest-<timestamp>)")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds to wait")
    ap.add_argument("--save", type=Path, help="also write the Job's log to this file")
    args = ap.parse_args()

    cron = json.loads(kubectl("get", "cronjob", "reindex", "-o", "json"))
    spec = cron["spec"]["jobTemplate"]["spec"]
    pod = spec["template"]["spec"]
    container = next(c for c in pod["containers"] if c["name"] == "indexer")
    extra: list[str] = []
    kind = "ingest"
    if args.count_by_doc:
        extra, kind = ["--count-by-doc"], "count"
    elif args.restore:
        extra, kind = ["--restore", args.restore], "restore"
    elif args.unpin:
        extra, kind = ["--unpin", args.unpin], "unpin"
    container["args"] = extra
    if args.config != "rahbar-config":
        container["envFrom"] = [{"configMapRef": {"name": args.config}}]
        container.setdefault("env", []).append({"name": "LAKE_READ_ONLY", "value": "true"})
    if kind != "ingest":
        # Only the ingest needs fresh documents from the web.
        pod.pop("initContainers", None)
    name = args.name or f"{kind}-{time.strftime('%m%d-%H%M%S')}"
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"name": name, "namespace": NS,
                        "labels": {"app.kubernetes.io/name": "indexer",
                                   "app.kubernetes.io/part-of": "rahbar",
                                   "rahbar/run": kind},
                        "annotations": {"cronjob.kubernetes.io/instantiate": "manual"}},
           "spec": spec}
    kubectl("apply", "-f", "-", stdin=json.dumps(job))
    print(f"job/{name} created from cronjob/reindex  (args: {extra or '[] = ingest'}, "
          f"config: {args.config})", flush=True)

    deadline = time.monotonic() + args.timeout
    status = "running"
    while time.monotonic() < deadline:
        s = json.loads(kubectl("get", "job", name, "-o", "json"))["status"]
        if s.get("succeeded"):
            status = "Complete"
            break
        if any(c["type"] == "Failed" and c["status"] == "True" for c in s.get("conditions", [])):
            status = "Failed"
            break
        time.sleep(5)
    logs = kubectl("logs", f"job/{name}", "-c", "indexer", "--tail=-1", check=False)
    # The JSON log lines are for machines; the tables are for people. Show both, tables last.
    print(logs)
    print(f"job/{name}: {status}")
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(f"$ python scripts/run_indexer.py {' '.join(sys.argv[1:])}\n"
                             f"{logs}\njob/{name}: {status}\n", encoding="utf-8")
    return 0 if status == "Complete" else 1


if __name__ == "__main__":
    sys.exit(main())
