"""Service 2: Claude Agent - standard RESTful API with Cognito OAuth.

Unlike Service 1 (a bespoke API wired as an HTTP Passthrough target), this
service follows conventional REST resource semantics around a `conversations`
resource, and ships a complete OpenAPI 3.0 spec (see ../openapi.yaml). In
AgentCore Gateway this is wired as an OpenAPI target: the gateway reads the
spec and auto-generates one MCP tool per operation.

    POST   /api/v1/conversations                 - create a conversation
    POST   /api/v1/conversations/{id}/messages   - send a message, get a reply
    GET    /api/v1/conversations/{id}            - fetch conversation history
    DELETE /api/v1/conversations/{id}            - delete a conversation
    GET    /api/v1/health                        - health check (unauthenticated)

The customer-service agent (app/agent.py) is backed by the Claude Agent SDK
running against Amazon Bedrock, with a deterministic mock fallback.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from .agent import run_chat
from .auth import AuthClaims

app = FastAPI(
    title="Claude Agent Service",
    version="1.0.0",
    description="Customer-service agent exposed via a standard RESTful API.",
)

# --- in-memory conversation store (mock) ------------------------------------
# A real service would use a database. For the demo a thread-safe in-process
# dict is enough. Each conversation holds an ordered list of messages.
_CONVERSATIONS: dict[str, dict] = {}
_CONVERSATIONS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- request/response models ------------------------------------------------

class ConversationCreated(BaseModel):
    conversation_id: str
    created_at: str


class MessageRequest(BaseModel):
    content: str = Field(..., description="User message for the agent.")


class Message(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    content: str


class ConversationHistory(BaseModel):
    conversation_id: str
    messages: list[Message]


# --- routes -----------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> dict:
    """Unauthenticated liveness probe used by k8s and the ALB."""
    return {"status": "ok", "service": "claude-agent"}


@app.post(
    "/api/v1/conversations",
    response_model=ConversationCreated,
    status_code=201,
)
async def create_conversation(claims: dict = AuthClaims) -> ConversationCreated:
    conversation_id = str(uuid.uuid4())
    created_at = _now_iso()
    with _CONVERSATIONS_LOCK:
        _CONVERSATIONS[conversation_id] = {
            "conversation_id": conversation_id,
            "created_at": created_at,
            "messages": [],
        }
    return ConversationCreated(conversation_id=conversation_id, created_at=created_at)


@app.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=Message,
    status_code=201,
)
async def send_message(
    conversation_id: str,
    req: MessageRequest,
    claims: dict = AuthClaims,
) -> Message:
    # Ensure the conversation exists before doing any work.
    with _CONVERSATIONS_LOCK:
        conversation = _CONVERSATIONS.get(conversation_id)
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        # Append the incoming user message.
        user_msg = {
            "message_id": str(uuid.uuid4()),
            "role": "user",
            "content": req.content,
        }
        conversation["messages"].append(user_msg)

    # Run the agent OUTSIDE the lock so concurrent conversations aren't blocked
    # by a potentially slow model call.
    reply = await run_chat(req.content)

    assistant_msg = {
        "message_id": str(uuid.uuid4()),
        "role": "assistant",
        "content": reply,
    }
    with _CONVERSATIONS_LOCK:
        # The conversation could theoretically have been deleted meanwhile.
        conversation = _CONVERSATIONS.get(conversation_id)
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        conversation["messages"].append(assistant_msg)

    return Message(**assistant_msg)


@app.get(
    "/api/v1/conversations/{conversation_id}",
    response_model=ConversationHistory,
)
async def get_conversation(
    conversation_id: str,
    claims: dict = AuthClaims,
) -> ConversationHistory:
    with _CONVERSATIONS_LOCK:
        conversation = _CONVERSATIONS.get(conversation_id)
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        messages = [Message(**m) for m in conversation["messages"]]
    return ConversationHistory(conversation_id=conversation_id, messages=messages)


@app.delete(
    "/api/v1/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: str,
    claims: dict = AuthClaims,
) -> Response:
    with _CONVERSATIONS_LOCK:
        existed = _CONVERSATIONS.pop(conversation_id, None)
    if existed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
