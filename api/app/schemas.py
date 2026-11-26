"""The contracts. Change them only with your partner's review: the UI, the
evaluation script and your tests all depend on these shapes.

`Answer` is what the model must produce (section 3.4). `AskResponse` is what
the API returns to the UI and to eval/run_eval.py. The model's `reasoning` is
never sent to the user.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    # A03: the evaluation suite asks with no_cache=true (every question must really run
    # the pipeline) and debug=true (the response then carries the context and stage
    # timings, which the faithfulness judge and the latency report need).
    no_cache: bool = False
    debug: bool = False


class RetrieveRequest(BaseModel):
    """POST /api/retrieve: retrieval only, no LLM (component-level tests, A03 B)."""

    question: str = Field(min_length=3, max_length=500)
    k: int | None = Field(default=None, ge=1, le=50)


class Passage(BaseModel):
    """One retrieved chunk, as the retriever hands it to the generator."""

    chunk_id: str
    text: str
    source: str
    section: str
    page: int | None = None
    score: float = Field(description="Similarity, higher is closer (1 - cosine distance).")
    # Internal only (never in AskResponse): lets the prompt label old versions.
    doc_version: str = ""
    superseded: bool = False
    doc_id: str = ""          # A03: for the trace (which document version produced this)


class Citation(BaseModel):
    source: str
    section: str
    page: int | None = None


class Answer(BaseModel):
    """The model's output contract. Send its JSON schema to the model, then validate."""

    reasoning: str
    answer: str
    citations: list[Citation]
    answered_from_context: bool
    confidence: Literal["high", "medium", "low"]


class RetrievedRef(BaseModel):
    """What the response says was retrieved. eval/run_eval.py scores against this."""

    chunk_id: str
    source: str
    section: str
    page: int | None = None
    score: float


class ContextPassage(BaseModel):
    """A passage exactly as it was put into the prompt (debug responses only)."""

    chunk_id: str
    source: str
    section: str
    page: int | None = None
    text: str


class AskResponse(BaseModel):
    request_id: str
    answer: str
    citations: list[Citation]
    answered_from_context: bool
    confidence: Literal["high", "medium", "low"]
    retrieved: list[RetrievedRef]
    provider: str
    # A03: the trace id (same as request_id) points at the trace record of this answer.
    trace_id: str = ""
    reason: str = ""                                   # answered | floor | model | ...
    context: list[ContextPassage] | None = None        # debug only
    timings_ms: dict[str, float] | None = None         # debug only


class RetrieveResponse(BaseModel):
    trace_id: str
    collection: str
    embed_model: str
    retrieved: list[RetrievedRef]
    timings_ms: dict[str, float]
