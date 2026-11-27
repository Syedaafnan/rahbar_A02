"""Hosted free-tier provider over an OpenAI-compatible API (Groq, OpenRouter, ...).

The key arrives as LLM_API_KEY from a Kubernetes Secret created on the command
line. It never appears in a file you commit, and never in a log line.
JSON mode forces a JSON object; the schema is in the system prompt; Pydantic validates.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...config import Settings
from ...routes import metrics
from ...schemas import Answer, Passage
from .prompt import build_messages


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        if not settings.hosted_base_url or not settings.llm_api_key:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL and the LLM_API_KEY Secret")
        self.settings = settings
        self.http = httpx.Client(timeout=settings.llm_timeout_seconds,
                                 headers={"Authorization": f"Bearer {settings.llm_api_key}"})

    def prompt(self, question: str, passages: list[Passage]) -> list[dict[str, str]]:
        return build_messages(question, passages)          # schema in the prompt

    def params(self) -> dict[str, Any]:
        return {"provider": "hosted", "base_url": self.settings.hosted_base_url,
                "model": self.settings.llm_model, "temperature": self.settings.temperature,
                "seed": self.settings.llm_seed, "response_format": "json_object"}

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        response = self.http.post(f"{self.settings.hosted_base_url.rstrip('/')}/chat/completions",
                                  json={
                                      "model": self.settings.llm_model,
                                      "messages": self.prompt(question, passages),
                                      "temperature": self.settings.temperature,
                                      "seed": self.settings.llm_seed,
                                      "response_format": {"type": "json_object"},
                                  })
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        metrics.TOKENS.labels(direction="in").observe(usage.get("prompt_tokens", 0))
        metrics.TOKENS.labels(direction="out").observe(usage.get("completion_tokens", 0))
        return Answer.model_validate_json(body["choices"][0]["message"]["content"])
