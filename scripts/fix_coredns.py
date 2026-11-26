#!/usr/bin/env python3
"""Make host.minikube.internal resolvable inside pods (the indexer, api and eval Jobs
call Ollama on the laptop through it). Adds a `hosts` block to CoreDNS if missing and
restarts CoreDNS. The IP comes from the minikube node's /etc/hosts."""

from __future__ import annotations

import json
import subprocess
import sys


def sh(*cmd: str, stdin: str | None = None) -> str:
    return subprocess.run(list(cmd), input=stdin, text=True, capture_output=True,
                          check=True, encoding="utf-8").stdout


def main() -> int:
    line = sh("minikube", "ssh", "grep host.minikube.internal /etc/hosts").split()
    ip = line[0]
    cm = json.loads(sh("kubectl", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json"))
    corefile = cm["data"]["Corefile"]
    if "host.minikube.internal" in corefile:
        print("CoreDNS already resolves host.minikube.internal")
        return 0
    cm["data"]["Corefile"] = corefile.replace(
        "    errors\n",
        f"    errors\n    hosts {{\n       {ip} host.minikube.internal\n       fallthrough\n    }}\n", 1)
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
        cm["metadata"].pop(k, None)
    print(sh("kubectl", "apply", "-f", "-", stdin=json.dumps(cm)).strip())
    print(sh("kubectl", "-n", "kube-system", "rollout", "restart", "deployment", "coredns").strip())
    print(sh("kubectl", "-n", "kube-system", "rollout", "status", "deployment", "coredns").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
