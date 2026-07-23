"""Service 1: LangGraph Agent - non-standard REST API with Cognito OAuth.

This deliberately does NOT follow the OpenAI or any standard schema. It exposes
a bespoke enterprise-style API:

    POST /api/v1/chat            - single-turn conversation
    POST /api/v1/tasks           - create an async task
    GET  /api/v1/tasks/{task_id} - poll task status
    GET  /api/v1/health          - health check (unauthenticated)

In AgentCore Gateway this is wired as an HTTP Passthrough (CUSTOM) target: the
gateway forwards requests as-is and authenticates outbound with a Cognito OAuth
credential provider.
"""

from __future__ import annotations

import threading
import uuid
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .agent import run_chat
from .auth import AuthClaims

app = FastAPI(
    title="LangGraph Agent Service",
    version="1.0.0",
    description="Enterprise ReAct agent exposed via a bespoke REST API.",
)

# --- in-memory async task store (mock) --------------------------------------
# A real service would use a queue + worker. For the demo an in-process thread
# and dict are enough to demonstrate the async task pattern.
_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


# --- request/response models ------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="User message for the agent.")


class ChatResponse(BaseModel):
    reply: str


class TaskRequest(BaseModel):
    message: str = Field(..., description="Prompt to process asynchronously.")


class TaskCreated(BaseModel):
    task_id: str
    status: Literal["pending", "running", "completed", "failed"]


class TaskStatus(BaseModel):
    task_id: str
    status: Literal["pending", "running", "completed", "failed"]
    result: str | None = None
    error: str | None = None


# --- routes -----------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> dict:
    """Unauthenticated liveness probe used by k8s and the ALB."""
    return {"status": "ok", "service": "langgraph-agent"}


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, claims: dict = AuthClaims) -> ChatResponse:
    reply = run_chat(req.message)
    return ChatResponse(reply=reply)


def _run_task(task_id: str, message: str) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id]["status"] = "running"
    try:
        result = run_chat(message)
        with _TASKS_LOCK:
            _TASKS[task_id].update(status="completed", result=result)
    except Exception as exc:  # noqa: BLE001
        with _TASKS_LOCK:
            _TASKS[task_id].update(status="failed", error=str(exc))


@app.post("/api/v1/tasks", response_model=TaskCreated, status_code=202)
async def create_task(req: TaskRequest, claims: dict = AuthClaims) -> TaskCreated:
    task_id = str(uuid.uuid4())
    with _TASKS_LOCK:
        _TASKS[task_id] = {"task_id": task_id, "status": "pending", "result": None, "error": None}
    # Fire-and-forget worker thread.
    threading.Thread(target=_run_task, args=(task_id, req.message), daemon=True).start()
    return TaskCreated(task_id=task_id, status="pending")


@app.get("/api/v1/tasks/{task_id}", response_model=TaskStatus)
async def get_task(task_id: str, claims: dict = AuthClaims) -> TaskStatus:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
    if task is None:
        from fastapi import HTTPException, status

        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return TaskStatus(**task)
