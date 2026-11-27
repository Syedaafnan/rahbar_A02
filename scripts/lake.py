#!/usr/bin/env python3
"""Move files in and out of the cluster's volumes (A03 Parts A, D, F).

A small helper pod (`lake-shell`, busybox, non-root) mounts the three PVCs:
    /data     corpus-data   the data lake (lake/raw, lake/quarantine, lake/archive, state, reports)
    /results  eval-results  evaluation result files and gate decisions
    /traces   traces        the api pods' trace files

    python scripts/lake.py push corpus/batch_b               # -> /data/lake/raw/batch_b
    python scripts/lake.py push ~/Downloads/new-docs --name demo --source "given at the demo"
    python scripts/lake.py put corpus/demos/b10-...v2.md batch_b/b10-student-clubs-and-societies-policy.md
    python scripts/lake.py rm batch_b/b03-laboratory-safety-policy.md
    python scripts/lake.py ls [lake/quarantine]
    python scripts/lake.py pull                              # results, traces, reports -> repo
    python scripts/lake.py shell-delete

The pod only copies files. The vector store is written by the indexer Job alone, so
after a push/put/rm, run the ingestion Job (python scripts/run_indexer.py).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NS = "rahbar"
POD = "lake-shell"

POD_SPEC = {
    "apiVersion": "v1", "kind": "Pod",
    "metadata": {"name": POD, "namespace": NS,
                 "labels": {"app.kubernetes.io/name": "lake-shell",
                            "app.kubernetes.io/part-of": "rahbar"}},
    "spec": {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "shell", "image": "busybox:1.36", "command": ["sleep", "86400"],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                          "limits": {"cpu": "200m", "memory": "64Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "data", "mountPath": "/data"},
                             {"name": "results", "mountPath": "/results"},
                             {"name": "traces", "mountPath": "/traces"}],
        }],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "corpus-data"}},
                    {"name": "results", "persistentVolumeClaim": {"claimName": "eval-results"}},
                    {"name": "traces", "persistentVolumeClaim": {"claimName": "traces"}}],
    },
}


def kubectl(*args: str, check: bool = True, capture: bool = False,
            stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], check=check, text=True, input=stdin,
                          capture_output=capture, encoding="utf-8")


def ensure_pod() -> None:
    got = kubectl("get", "pod", POD, "-n", NS, "-o", "jsonpath={.status.phase}",
                  check=False, capture=True)
    if got.returncode == 0 and got.stdout.strip() == "Running":
        return
    if got.returncode != 0:
        kubectl("apply", "-f", "-", stdin=json.dumps(POD_SPEC))
    kubectl("wait", "--for=condition=Ready", f"pod/{POD}", "-n", NS, "--timeout=120s")


def sh(command: str) -> str:
    return kubectl("exec", "-n", NS, POD, "--", "sh", "-c", command, capture=True).stdout


def local(path: Path | str) -> str:
    """kubectl cp reads 'C:/...' as pod 'C' and path '/...': pass local paths relative."""
    return os.path.relpath(Path(path).resolve(), Path.cwd().resolve())


def cmd_push(args: argparse.Namespace) -> None:
    src = Path(args.folder)
    name = args.name or src.name
    files = [f for f in sorted(src.iterdir()) if f.is_file() and f.suffix != ".py"]
    if not files:
        sys.exit(f"no files in {src}")
    ensure_pod()
    target = f"/data/lake/raw/{name}"
    sh(f"mkdir -p '{target}'")
    with tempfile.TemporaryDirectory() as tmp:
        manifest = src / "_batch.yaml"
        if not manifest.exists() and args.source:
            # The operator declares where the batch came from; title and date still have
            # to come from the documents themselves (or verification will say so).
            data = {"documents": [{"file": f.name, "source": args.source} for f in files]}
            extra = Path(tmp) / "_batch.yaml"
            extra.write_text(yaml.safe_dump(data), encoding="utf-8")
            files.append(extra)
        for f in files:
            kubectl("cp", local(f), f"{NS}/{POD}:{target}/{f.name}")
            print(f"  pushed {f.name}")
    print(f"\n{len(files)} files in {target}:\n{sh(f'ls -la {target!r}')}")
    print("next: python scripts/run_indexer.py   (the ingestion Job verifies and indexes them)")


def cmd_put(args: argparse.Namespace) -> None:
    ensure_pod()
    remote = f"/data/lake/raw/{args.lake_path}"
    sh(f"mkdir -p \"$(dirname '{remote}')\"")
    kubectl("cp", local(args.file), f"{NS}/{POD}:{remote}")
    print(f"put {args.file} -> {remote}")


def cmd_rm(args: argparse.Namespace) -> None:
    ensure_pod()
    remote = f"/data/lake/raw/{args.lake_path}"
    print(sh(f"ls -la '{remote}' && rm -f '{remote}' && echo removed '{remote}'"))


def cmd_ls(args: argparse.Namespace) -> None:
    ensure_pod()
    print(sh(f"cd /data && find {args.path or 'lake'} -maxdepth 4 | sort"))


def cmd_pull(args: argparse.Namespace) -> None:
    """Copy results, traces and ingestion reports from the volumes into the repo, where
    they are committed (the assignment: eval/results/ and traces/)."""
    ensure_pod()
    targets = [("/results", ROOT / "eval" / "results" / "cluster"),
               ("/traces", ROOT / "traces"),
               ("/data/reports", ROOT / "eval" / "results" / "ingest-reports")]
    for remote, dest in targets:
        dest.mkdir(parents=True, exist_ok=True)
        # latest-<label>.json are copies of timestamped files (the gate Job reads them on
        # the volume); the repo keeps only the timestamped originals.
        names = [n for n in sh(f"ls -1 {remote} 2>/dev/null || true").split()
                 if not n.startswith("latest-")]
        for n in names:
            kubectl("cp", f"{NS}/{POD}:{remote}/{n}", local(dest / n))
        print(f"  {remote:<14} -> {dest.relative_to(ROOT)}  ({len(names)} files)")
    reg = sh("cat /data/state/rahbar/registry.json 2>/dev/null || true")
    if reg.strip():
        out = ROOT / "eval" / "results" / "ingest-reports" / "registry-rahbar.json"
        out.write_text(reg, encoding="utf-8")
        print(f"  registry       -> {out.relative_to(ROOT)}")


def cmd_report(args: argparse.Namespace) -> None:
    """Print a stored verification report (reports/verify-<run>.json on the PVC) as the
    pass/fail table, with every failure reason: the A1 output, any time after the run."""
    ensure_pod()
    names = sorted(sh("ls -1 /data/reports | grep '^verify-' || true").split())
    if args.run:
        names = [n for n in names if args.run in n]
    if args.batch:
        names = [n for n in names if f'"batch": "{args.batch}"' in sh(f"cat /data/reports/{n}")]
    if not names:
        sys.exit("no matching verification report in /data/reports")
    name = names[0] if args.run or args.batch else names[-1]
    rep = json.loads(sh(f"cat /data/reports/{name}"))
    checks = ("quality", "completeness", "recency", "uniqueness")
    mark = {True: "PASS", False: "FAIL", None: "n/a"}
    print(f"VERIFICATION REPORT  /data/reports/{name}  collection={rep['collection']}  "
          f"model={rep['embed_model']}  near-dup threshold={rep['near_dup_threshold']}\n")
    print(f"{'document':<44} {'batch':<8} " + " ".join(f"{c:<12}" for c in checks) + " decision")
    for d in rep["documents"]:
        if args.only_batch and d["batch"] != args.only_batch:
            continue
        cells = " ".join(f"{mark[d['checks'][c]['pass']]:<12}" for c in checks)
        print(f"{d['source'][:44]:<44} {d['batch'][:8]:<8} {cells} {d['decision']}")
        for c in checks:
            if d["checks"][c]["pass"] is False:
                print(f"      {c}: {d['checks'][c]['reason']}")
    s = rep.get("ingest", {})
    if s:
        print(f"\nseen {s['documents_seen']}, indexed {s['indexed_active']}, archived {s['archived']}, "
              f"quarantined {s['quarantined']}; chunks embedded {s['embedded']}, skipped "
              f"{s['skipped_unchanged']}, deleted {s['deleted']}; collection holds {s['total']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Files in and out of the RAHBAR volumes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="copy a local folder into the lake as a new batch")
    p.add_argument("folder")
    p.add_argument("--name", help="batch name in the lake (default: the folder name)")
    p.add_argument("--source", help="declare the source of every file (when there is no _batch.yaml)")
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("put", help="copy one file to lake/raw/<lake_path> (e.g. a changed version)")
    p.add_argument("file")
    p.add_argument("lake_path")
    p.set_defaults(fn=cmd_put)
    p = sub.add_parser("rm", help="delete lake/raw/<lake_path> (the deletion demo)")
    p.add_argument("lake_path")
    p.set_defaults(fn=cmd_rm)
    p = sub.add_parser("ls", help="list the data lake")
    p.add_argument("path", nargs="?")
    p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("report", help="print a verification report from the PVC (A1)")
    p.add_argument("--run", help="run id, e.g. 20261125T042629Z (default: the newest)")
    p.add_argument("--batch", help="the first report that contains this batch")
    p.add_argument("--only-batch", help="show only this batch's documents")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("pull", help="copy results, traces and reports into the repository")
    p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("shell-delete", help="remove the helper pod")
    p.set_defaults(fn=lambda a: kubectl("delete", "pod", POD, "-n", NS, "--ignore-not-found"))
    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
