"""Service 3: Enterprise Knowledge-Base MCP Server.

An MCP server built on the official Python MCP SDK (`mcp` package) using the
Streamable HTTP transport (a single `/mcp` endpoint). It exposes four tools,
all backed by in-memory MOCK data:

    search_knowledge_base(query)          - search internal docs
    get_document(doc_id)                  - fetch a full document
    list_recent_updates(limit=5)          - most recently updated docs
    submit_feedback(doc_id, feedback)     - record feedback, return an ack id

Inbound auth follows the MCP spec 2025-06-18 (OAuth 2.1). Every request to the
`/mcp` mount must carry a valid Cognito access token in the
`Authorization: Bearer <token>` header; this makes the server an OAuth 2.1
protected resource. AgentCore Gateway satisfies this on the OUTBOUND side using
its Cognito OAuth credential provider. A separate, unauthenticated `GET /health`
route is exposed for k8s and ALB probes.

The MCP streamable-http app and the health route are combined under a single
Starlette parent app so one uvicorn process on port 8080 serves both.
"""

# NOTE: `from __future__ import annotations` is intentionally NOT used here.
# FastMCP introspects tool parameter annotations at registration time and does
# not resolve stringized (PEP 563) annotations, so the type hints must remain
# real objects.
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import tools
from .auth import require_auth

# --- MCP server + tools -----------------------------------------------------
# stateless_http=True: each request is self-contained (no server-side session
# affinity), which suits horizontal scaling behind an ALB and AgentCore Gateway.
mcp = FastMCP("enterprise-kb", stateless_http=True)


@mcp.tool()
def search_knowledge_base(query: str) -> dict:
    """Search the internal knowledge base and return matching doc titles + snippets."""
    return tools.search_knowledge_base(query)


@mcp.tool()
def get_document(doc_id: str) -> dict:
    """Return the full content of a knowledge-base document by its id."""
    return tools.get_document(doc_id)


@mcp.tool()
def list_recent_updates(limit: int = 5) -> dict:
    """List the most recently updated knowledge-base documents (default 5)."""
    return tools.list_recent_updates(limit)


@mcp.tool()
def submit_feedback(doc_id: str, feedback: str) -> dict:
    """Record feedback for a document and return an acknowledgement id."""
    return tools.submit_feedback(doc_id, feedback)


# --- inbound OAuth 2.1 auth middleware --------------------------------------
# Applied only to the /mcp mount. Enforces a valid Cognito access token on every
# MCP request, satisfying the MCP 2025-06-18 OAuth 2.1 resource-server
# expectations. The /health route is mounted outside this middleware and stays
# unauthenticated for k8s/ALB probes.
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # require_auth raises a Starlette HTTPException (401/403) on failure.
        # BaseHTTPMiddleware runs outside Starlette's exception handlers, so we
        # convert the exception into a JSON response here rather than letting it
        # propagate (which would surface as a 500).
        try:
            await require_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                {"error": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        return await call_next(request)


async def health(_request: Request) -> JSONResponse:
    """Unauthenticated liveness/readiness probe used by k8s and the ALB."""
    return JSONResponse({"status": "ok", "service": "mcp-server"})


# The FastMCP streamable-http app serves the transport at its own `/mcp` path.
# Mounting it at the root keeps that endpoint reachable as `/mcp`, and wrapping
# it with BearerAuthMiddleware protects only the MCP traffic.
mcp_app = mcp.streamable_http_app()
mcp_app.add_middleware(BearerAuthMiddleware)

# The streamable-http session manager must be started/stopped with the app
# lifecycle. FastMCP wires this into the mcp_app's lifespan, so the parent app
# must delegate to it or the session manager is never initialized.
app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lambda _app: mcp_app.router.lifespan_context(_app),
)
