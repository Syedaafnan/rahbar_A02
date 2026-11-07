"""What /readyz checks (sections 4.3 and 4.4), and the embedding-pin guardrail.

Ready means ALL of these hold:
  chroma     reachable, the collection exists and holds at least one chunk
  embedding  the model recorded in the collection metadata == our EMBED_MODEL
  redis      answers PING (only when the cache is enabled)

If the indexer built the index with another embedding model, query vectors and
document vectors live in different spaces and retrieval returns nonsense with no
error anywhere. So a mismatch keeps the pod out of the Service (0/1 READY) and
logs exactly what conflicts, instead of silently serving garbage.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from ..config import Settings
from ..retrieval.base import Retriever
from .cache import Cache

log = logging.getLogger("rahbar.readiness")

# Every dependency call gets a hard deadline, shorter than the probe's timeoutSeconds
# (3 s in k8s/base/api.yaml): a hanging Chroma must make /readyz say "not ready"
# quickly, never make the probe itself hang.
DEPENDENCY_TIMEOUT = 2.5
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="readiness")


def _with_timeout(fn: Any) -> Any:
    return _pool.submit(fn).result(timeout=DEPENDENCY_TIMEOUT)


def check(settings: Settings, retriever: Retriever, cache: Cache) -> dict[str, str]:
    """Return {dependency: "ok" or the reason it failed}."""
    checks: dict[str, str] = {}
    try:
        info: dict[str, Any] = _with_timeout(retriever.index_info)
        if info["count"] <= 0:
            checks["chroma"] = f"collection '{settings.collection}' is empty: run the indexer Job"
        else:
            checks["chroma"] = "ok"
        checks["embedding"] = pin_problem(settings.embed_model, info["metadata"]) or "ok"
    except FutureTimeout:
        checks["chroma"] = f"no answer within {DEPENDENCY_TIMEOUT}s"
        checks["embedding"] = "unknown (chroma not available)"
    except Exception as exc:
        checks["chroma"] = f"unreachable or no collection '{settings.collection}': " \
                           f"{type(exc).__name__}: {str(exc)[:120]}"
        checks["embedding"] = "unknown (chroma not available)"
    if getattr(cache, "enabled", False):
        try:
            checks["redis"] = "ok" if _with_timeout(cache.ping) else "no PONG"
        except FutureTimeout:
            checks["redis"] = f"no answer within {DEPENDENCY_TIMEOUT}s"
        except Exception as exc:
            checks["redis"] = f"unreachable: {type(exc).__name__}"
    return checks


def check_in_background(app_state: Any) -> None:
    """The startup pin check (section 4.4), run off the startup path. The server
    starts listening at once (so the startup and liveness probes pass), the result
    is logged as soon as Chroma answers, and /readyz repeats the check on every probe."""
    import threading

    def run() -> None:
        app_state.last_readiness = check(app_state.settings, app_state.retriever,
                                         app_state.cache)
        log_result(app_state.last_readiness, None)

    threading.Thread(target=run, name="startup-pin-check", daemon=True).start()


def pin_problem(api_model: str, metadata: dict[str, Any]) -> str | None:
    """None when the pin holds, else a sentence naming both sides of the conflict."""
    if metadata.get("embed_model") == "stub":
        return None
    index_model = metadata.get("embed_model")
    if index_model != api_model:
        return (f"embedding model mismatch: index built with '{index_model}' "
                f"(dimension {metadata.get('dimension')}), api configured with "
                f"EMBED_MODEL='{api_model}'. Re-run the indexer or fix the ConfigMap.")
    return None


def log_result(checks: dict[str, str], previous: dict[str, str] | None) -> None:
    """Log at startup and whenever the result changes, not on every 10 s probe."""
    if checks == previous:
        return
    failed = {k: v for k, v in checks.items() if v != "ok"}
    if failed:
        log.error("not ready", extra={"failed": failed})
    else:
        log.info("ready", extra={"checks": checks})
