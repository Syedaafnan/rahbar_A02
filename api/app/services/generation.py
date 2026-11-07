"""Generate, validate, retry once, then fall back (section 3.4, step 7).

Structure can be enforced; truth cannot. A reply that is not valid JSON for the
Answer schema, or a provider error (timeout, 5xx), gets exactly one retry. If the
second attempt fails too, the user gets a safe canned answer instead of a 500.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError

from ..providers.llm.base import LLMProvider
from ..routes import metrics
from ..schemas import Answer, Passage
from .grounding import fallback_answer

log = logging.getLogger("rahbar.generation")


def generate_with_retry(provider: LLMProvider, question: str,
                        passages: list[Passage]) -> tuple[Answer, bool]:
    """Return (answer, used_fallback)."""
    for attempt in (1, 2):
        try:
            with metrics.GENERATION_SECONDS.time():
                return provider.generate(question, passages), False
        except (ValidationError, ValueError, KeyError, httpx.HTTPError) as exc:
            log.warning("llm reply rejected, retrying once" if attempt == 1
                        else "llm reply rejected again, using fallback",
                        extra={"attempt": attempt,
                                                     "error": type(exc).__name__,
                                                     "detail": str(exc)[:300]})
            if attempt == 1:
                metrics.LLM_RETRIES.inc()
    metrics.FALLBACKS.inc()
    return fallback_answer(), True
