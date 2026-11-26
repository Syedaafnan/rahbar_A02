#!/usr/bin/env python3
"""Controlled release of a pipeline change on Kubernetes (A03 C3, F3).

The live api Deployment reads its whole configuration (embedding model, prefixes,
collection) from ONE ConfigMap named in its pod template. A release changes that
name, which changes the pod template, which makes Kubernetes do a rolling update
(maxSurge 1, maxUnavailable 0) and record a new revision. So `kubectl rollout undo`
brings back the previous ConfigMap, and with it the previous collection and model.

    python scripts/release.py stage good|bad     # point api-candidate at a candidate
    python scripts/release.py evaluate good|bad  # run the Part B suite Job against it
    python scripts/release.py promote good       # roll out, ONLY if its eval Job passed
    python scripts/release.py promote bad        # refused: the gate stopped it
    python scripts/release.py undo               # kubectl rollout undo deployment/api
    python scripts/release.py ask "question"     # one question to the live api + its trace
    python scripts/release.py status             # which ConfigMap is live, rollout history

Every step prints the kubectl command it runs, so the screenshot shows the real thing.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NS = "rahbar"
ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = {
    "good": {"config": "rahbar-config-candidate", "job": "eval-candidate",
             "manifest": "k8s/eval-candidate-job.yaml"},
    "bad": {"config": "rahbar-config-bad", "job": "eval-bad",
            "manifest": "k8s/eval-bad-job.yaml"},
}


def run(*cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print("$ " + " ".join(cmd), flush=True)
    out = subprocess.run(list(cmd), text=True, capture_output=True, encoding="utf-8")
    if not quiet and (out.stdout or out.stderr):
        print((out.stdout + out.stderr).rstrip(), flush=True)
    if check and out.returncode != 0:
        sys.exit(out.returncode)
    return out


def kget(*args: str) -> dict:
    out = run("kubectl", "-n", NS, "get", *args, "-o", "json", quiet=True, check=False)
    return json.loads(out.stdout) if out.returncode == 0 else {}


def live_config(deployment: str = "api") -> str:
    d = kget("deployment", deployment)
    return d["spec"]["template"]["spec"]["containers"][0]["envFrom"][0]["configMapRef"]["name"]


def set_config(deployment: str, config: str, cause: str) -> None:
    patch = json.dumps([{"op": "replace",
                         "path": "/spec/template/spec/containers/0/envFrom/0/configMapRef/name",
                         "value": config}])
    run("kubectl", "-n", NS, "patch", "deployment", deployment, "--type", "json", "-p", patch)
    run("kubectl", "-n", NS, "annotate", "deployment", deployment, "--overwrite",
        f"kubernetes.io/change-cause={cause}")


def job_state(name: str) -> str:
    s = kget("job", name).get("status", {})
    if not s:
        return "missing"
    if s.get("succeeded"):
        return "passed"
    if any(c["type"] == "Failed" and c["status"] == "True" for c in s.get("conditions", [])):
        return "failed"
    return "running"


def cmd_stage(a: argparse.Namespace) -> int:
    c = CANDIDATES[a.which]
    set_config("api-candidate", c["config"], f"stage {a.which} candidate ({c['config']})")
    run("kubectl", "-n", NS, "rollout", "status", "deployment/api-candidate", "--timeout=300s")
    return 0


def cmd_evaluate(a: argparse.Namespace) -> int:
    c = CANDIDATES[a.which]
    if live_config("api-candidate") != c["config"]:
        print(f"api-candidate serves {live_config('api-candidate')}, not {c['config']}: "
              f"run `release.py stage {a.which}` first")
        return 2
    run("kubectl", "-n", NS, "delete", "job", c["job"], "--ignore-not-found")
    run("kubectl", "apply", "-f", str(ROOT / c["manifest"]))
    print(f"waiting for job/{c['job']} (the suite takes a while with a CPU-only LLM) ...")
    while job_state(c["job"]) == "running":
        time.sleep(15)
    logs = run("kubectl", "-n", NS, "logs", f"job/{c['job']}", "--tail=60", quiet=True,
               check=False).stdout
    print(logs)
    state = job_state(c["job"])
    print(f"job/{c['job']}: {'Complete (gate PASSED)' if state == 'passed' else 'Failed (gate REJECTED)'}")
    return 0 if state == "passed" else 1


def cmd_promote(a: argparse.Namespace) -> int:
    c = CANDIDATES[a.which]
    state = job_state(c["job"])
    print(f"release gate for the {a.which} candidate: job/{c['job']} is {state}")
    if state != "passed" and not a.force:
        print(f"REFUSED: {c['config']} is not rolled out. The Part B suite Job must complete "
              f"(gate passed) first; the live api keeps serving {live_config()}.")
        run("kubectl", "-n", NS, "get", "job", c["job"], check=False)
        return 1
    if state != "passed":
        print("--force: rolling out WITHOUT a passed gate (operator override)")
    before = live_config()
    set_config("api", c["config"], f"release {a.which} candidate: {before} -> {c['config']}")
    out = run("kubectl", "-n", NS, "rollout", "status", "deployment/api",
              f"--timeout={a.timeout}s", check=False)
    if out.returncode != 0:
        print("rollout did not finish: maxUnavailable 0 keeps the old pods serving. "
              "Reverse it with: python scripts/release.py undo")
        return 1
    run("kubectl", "-n", NS, "rollout", "history", "deployment/api")
    return 0


def cmd_undo(a: argparse.Namespace) -> int:
    print(f"live api currently reads {live_config()}")
    run("kubectl", "-n", NS, "rollout", "undo", "deployment/api")
    run("kubectl", "-n", NS, "rollout", "status", "deployment/api", f"--timeout={a.timeout}s")
    print(f"live api now reads {live_config()}")
    return 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def cmd_ask(a: argparse.Namespace) -> int:
    """One question to the live Service (via port-forward), then the trace fields that
    show which release answered: collection, index version, embedding model/version."""
    port = _free_port()
    pf = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"svc/{a.service}",
                           f"{port}:8000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        body = None
        for _ in range(30):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/{a.endpoint}",
                    data=json.dumps({"question": a.question, "no_cache": True}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=400) as r:
                    body = json.loads(r.read())
                break
            except OSError:
                time.sleep(1)
        if body is None:
            print("could not reach the api")
            return 1
        trace_id = body.get("trace_id")
        print(f"trace_id: {trace_id}")
        if a.endpoint == "ask":
            print(f"answer: {body.get('answer')}")
        time.sleep(1)
        logs = run("kubectl", "-n", NS, "logs", "-l", f"app.kubernetes.io/name={a.service}",
                   "--tail=400", "--prefix", quiet=True, check=False).stdout
        for line in logs.splitlines():
            if trace_id and trace_id in line and '"type": "trace"' in line:
                pod, _, raw = line.partition(" ")
                rec = json.loads(raw)
                print(f"\n$ kubectl -n {NS} logs -l app.kubernetes.io/name={a.service} | "
                      f"grep {trace_id}\n{pod}")
                print(json.dumps({k: rec[k] for k in ("trace_id", "timestamp", "release", "pod",
                                                      "embedding", "collection")}, indent=1))
                break
        return 0
    finally:
        pf.terminate()


def cmd_status(a: argparse.Namespace) -> int:
    print(f"live api        reads ConfigMap {live_config()}")
    if kget("deployment", "api-candidate"):
        print(f"api-candidate   reads ConfigMap {live_config('api-candidate')}")
    for which, c in CANDIDATES.items():
        print(f"gate ({which:<4})     job/{c['job']}: {job_state(c['job'])}")
    run("kubectl", "-n", NS, "rollout", "history", "deployment/api")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate-controlled release of the api.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("stage", cmd_stage), ("evaluate", cmd_evaluate), ("promote", cmd_promote)):
        p = sub.add_parser(name)
        p.add_argument("which", choices=sorted(CANDIDATES))
        p.set_defaults(fn=fn)
        if name == "promote":
            p.add_argument("--force", action="store_true",
                           help="roll out even though the gate did not pass (override)")
            p.add_argument("--timeout", type=int, default=600)
    p = sub.add_parser("undo")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_undo)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--service", default="api")
    p.add_argument("--endpoint", default="retrieve", choices=["retrieve", "ask"],
                   help="retrieve (fast, no LLM) is enough to show the release in the trace")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("status")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
