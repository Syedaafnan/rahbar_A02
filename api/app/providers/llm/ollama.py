"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

Enforce, then validate (section 3.4): Ollama's `format` parameter takes the Answer
JSON schema and constrains decoding to it, then Pydantic validates the reply anyway.
A reply that fails validation raises; services/generation.py retries once and then
falls back to a safe answer, so the user never sees a 500.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...config import Settings
from ...routes import metrics
from ...schemas import Answer, Passage
from .prompt import build_messages


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.Client(timeout=settings.llm_timeout_seconds)

    def prompt(self, question: str, passages: list[Passage]) -> list[dict[str, str]]:
        # format= in generate() enforces the schema, so the prompt need not repeat it.
        return build_messages(question, passages, include_schema=False)

    def params(self) -> dict[str, Any]:
        # A fixed seed with a low temperature: the same evidence gives the same answer,
        # which the stochasticity test (A03 B) relies on.
        return {"provider": "ollama", "model": self.settings.llm_model,
                "temperature": self.settings.temperature, "seed": self.settings.llm_seed,
                "num_ctx": 4096, "format": "Answer JSON schema"}

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        response = self.http.post(f"{self.settings.ollama_url}/api/chat", json={
            "model": self.settings.llm_model,
            "messages": self.prompt(question, passages),
            "format": Answer.model_json_schema(),
            "stream": False,
            # Keep the model in memory between questions: on our laptop a cold load took
            # ~35 s and, with little free RAM, sometimes failed with a 500.
            "keep_alive": "30m",
            "options": {"temperature": self.settings.temperature, "num_ctx": 4096,
                        "seed": self.settings.llm_seed},
        })
        response.raise_for_status()
        body = response.json()
        metrics.TOKENS.labels(direction="in").observe(body.get("prompt_eval_count", 0))
        metrics.TOKENS.labels(direction="out").observe(body.get("eval_count", 0))
        return Answer.model_validate_json(body["message"]["content"])
