"""HTTP surface for the BLM assistant — /api/v4/assistant/*.

Mounted under the same global auth middleware as the rest of /api/v4, so only
signed-in dashboard users reach it — no separate login, no new secret.

The route is a plain ``def`` on purpose: FastAPI runs sync handlers in its
threadpool, so a slow model call cannot block the event loop that serves the
trading pipeline.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from blm_v4.assistant import agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v4/assistant", tags=["blm-assistant"])

MAX_TURNS = 12          # history messages accepted from the client
MAX_QUESTION = 4000


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_QUESTION)
    history: list[ChatMessage] = Field(default_factory=list)


class ToolCall(BaseModel):
    tool: str
    input: dict = Field(default_factory=dict)
    preview: str = ""


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model: str = ""


@router.get("/status")
def assistant_status() -> dict:
    """Whether the assistant is usable, and which model it will use."""
    ok, why = agent.available()
    return {"available": ok, "reason": why,
            "model": agent.credentials()["model"] if ok else "",
            "read_only": True}


@router.post("/chat", response_model=ChatResponse)
def assistant_chat(req: ChatRequest) -> ChatResponse:
    """Answer one question about the BLM, using read-only tools."""
    hist = [m.model_dump() for m in req.history[-MAX_TURNS:]]
    try:
        out = agent.answer(req.message, history=hist)
    except Exception as e:
        logger.warning("assistant_chat_failed: %s", f"{type(e).__name__}: {e}"[:200])
        raise HTTPException(status_code=502,
                            detail=f"assistant failed: {type(e).__name__}")
    return ChatResponse(
        reply=out.get("reply") or "(no reply)",
        tool_calls=[ToolCall(**t) for t in (out.get("tool_calls") or [])],
        model=out.get("model", ""),
    )
