"""POST /api/ask and POST /api/retrieve. The Ingress sends /api/* here unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..schemas import AskRequest, AskResponse, RetrieveRequest, RetrieveResponse
from ..services.answer import answer_question, retrieve_only

router = APIRouter(prefix="/api")


@router.post("/ask", response_model=AskResponse, response_model_exclude_none=True)
def ask(body: AskRequest, request: Request, response: Response) -> AskResponse:
    state = request.app.state
    result, cache_status = answer_question(body.question, state.retriever, state.provider,
                                           state.settings, state.cache,
                                           no_cache=body.no_cache, debug=body.debug)
    response.headers["X-Cache"] = cache_status     # the UI shows this as a chip
    response.headers["X-Trace-ID"] = result.trace_id
    return result


@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(body: RetrieveRequest, request: Request, response: Response) -> RetrieveResponse:
    """Retrieval only, no LLM: for the component-level evaluation (A03 B)."""
    state = request.app.state
    result = retrieve_only(body.question, state.retriever, state.settings, k=body.k)
    response.headers["X-Trace-ID"] = result.trace_id
    return result
