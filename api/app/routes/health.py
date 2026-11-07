"""Liveness and readiness (section 4.3). They answer different questions."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..services import readiness

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM: if it did,
    one slow dependency would make the kubelet restart every pod at once."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Can this pod take traffic? 503, naming the failed dependency, unless Chroma is
    reachable with a non-empty collection, the embedding pin holds, and Redis answers."""
    state = request.app.state
    checks = readiness.check(state.settings, state.retriever, state.cache)
    readiness.log_result(checks, getattr(state, "last_readiness", None))
    state.last_readiness = checks
    if any(v != "ok" for v in checks.values()):
        response.status_code = 503
        return {"status": "not ready", "checks": checks}
    return {"status": "ready", "checks": checks}
