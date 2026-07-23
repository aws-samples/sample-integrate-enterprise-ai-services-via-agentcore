"""Service 2: Customer-service agent built on the Anthropic Claude Agent SDK.

The agent answers customer-service questions (order status, returns, product
recommendations) by driving a tool loop over a set of in-process SDK tools.
All data is self-contained MOCK data - there are no external data sources - so
the demo runs anywhere without extra credentials or network access.

Bedrock wiring
--------------
The Claude Agent SDK runs the Claude Code CLI as a subprocess. To make that
subprocess talk to Amazon Bedrock instead of the Anthropic public API we set
three environment variables (see the container spec / k8s deployment):

    CLAUDE_CODE_USE_BEDROCK=1   -> route model calls through Amazon Bedrock
    ANTHROPIC_MODEL=<bedrock-model-id>  -> e.g. apac.anthropic.claude-sonnet-4-5-...
    AWS_REGION=<region>         -> region whose Bedrock endpoint is used

Credentials are resolved by the standard AWS chain (IRSA / instance role /
env), so no secrets are hard-coded here. This module only *reads* those env
vars to build the SDK options; it never sets or logs credentials.

Graceful fallback
-----------------
The real Claude Agent SDK path is the default. If the SDK package is not
importable (e.g. the Claude Code CLI / Node runtime is missing) OR the operator
sets USE_MOCK_AGENT=true, we fall back to a deterministic reply that still
exercises the same mock tools directly, so the REST API stays fully functional
for demos and CI.
"""

from __future__ import annotations

import json
import os
from typing import Any

# --- Bedrock / model configuration ------------------------------------------

_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
# ANTHROPIC_MODEL is the Bedrock model id consumed by the Claude Code CLI when
# CLAUDE_CODE_USE_BEDROCK=1. Fall back to BEDROCK_MODEL_ID for parity with
# Service 1, then to a sensible default.
_MODEL_ID = (
    os.environ.get("ANTHROPIC_MODEL")
    or os.environ.get("BEDROCK_MODEL_ID")
    or "apac.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
_USE_BEDROCK = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip() in ("1", "true", "True")

# Explicit operator override for the deterministic mock path.
_USE_MOCK_AGENT = os.environ.get("USE_MOCK_AGENT", "false").lower() == "true"

_SYSTEM_PROMPT = (
    "You are a helpful customer-service assistant for an online store. "
    "Use the available tools to look up orders, create product returns, and "
    "recommend products. Only use information returned by the tools; do not "
    "invent order ids, prices, or availability. Be concise and friendly."
)


# --- Mock data ---------------------------------------------------------------
# Self-contained mock store. A real service would query an OMS / catalog.

_ORDERS: dict[str, dict[str, Any]] = {
    "ORD-1001": {
        "order_id": "ORD-1001",
        "customer": "Alice Nguyen",
        "status": "shipped",
        "items": [{"sku": "SKU-COFFEE-01", "name": "Single-origin coffee beans", "qty": 2, "price": 18.0}],
        "total": 36.0,
        "placed_at": "2026-07-15",
    },
    "ORD-1002": {
        "order_id": "ORD-1002",
        "customer": "Bob Martins",
        "status": "processing",
        "items": [{"sku": "SKU-MUG-02", "name": "Ceramic pour-over mug", "qty": 1, "price": 24.0}],
        "total": 24.0,
        "placed_at": "2026-07-20",
    },
    "ORD-1003": {
        "order_id": "ORD-1003",
        "customer": "Carla Diaz",
        "status": "delivered",
        "items": [{"sku": "SKU-GRINDER-03", "name": "Hand coffee grinder", "qty": 1, "price": 45.0}],
        "total": 45.0,
        "placed_at": "2026-07-01",
    },
    "ORD-1004": {
        "order_id": "ORD-1004",
        "customer": "Dan O'Brien",
        "status": "cancelled",
        "items": [{"sku": "SKU-FILTER-04", "name": "Reusable paper filters", "qty": 3, "price": 6.0}],
        "total": 18.0,
        "placed_at": "2026-06-28",
    },
}

_PRODUCT_CATALOG: dict[str, list[dict[str, Any]]] = {
    "coffee": [
        {"sku": "SKU-COFFEE-01", "name": "Single-origin coffee beans", "price": 18.0, "in_stock": True},
        {"sku": "SKU-COFFEE-05", "name": "Decaf house blend", "price": 16.0, "in_stock": True},
    ],
    "accessories": [
        {"sku": "SKU-MUG-02", "name": "Ceramic pour-over mug", "price": 24.0, "in_stock": True},
        {"sku": "SKU-GRINDER-03", "name": "Hand coffee grinder", "price": 45.0, "in_stock": False},
        {"sku": "SKU-FILTER-04", "name": "Reusable paper filters", "price": 6.0, "in_stock": True},
    ],
}

# In-memory returns ledger, keyed by generated return id.
_RETURNS: dict[str, dict[str, Any]] = {}


# --- Pure mock tool implementations -----------------------------------------
# These plain functions hold the business logic. Both the SDK @tool wrappers
# and the deterministic fallback call into them, so behaviour is identical on
# either path.

def _lookup_order(order_id: str) -> dict[str, Any]:
    order = _ORDERS.get(order_id)
    if order is None:
        return {"error": f"Order {order_id!r} not found"}
    return order


def _make_return(order_id: str, reason: str) -> dict[str, Any]:
    order = _ORDERS.get(order_id)
    if order is None:
        return {"error": f"Order {order_id!r} not found"}
    if order["status"] == "cancelled":
        return {"error": f"Order {order_id} was cancelled and cannot be returned"}
    return_id = f"RET-{len(_RETURNS) + 1:04d}"
    record = {
        "return_id": return_id,
        "order_id": order_id,
        "reason": reason,
        "status": "return_requested",
    }
    _RETURNS[return_id] = record
    return record


def _list_recommendations(category: str) -> dict[str, Any]:
    key = (category or "").strip().lower()
    products = _PRODUCT_CATALOG.get(key)
    if products is None:
        return {
            "error": f"Unknown category {category!r}",
            "available_categories": sorted(_PRODUCT_CATALOG.keys()),
        }
    # Recommend in-stock items first.
    recommended = sorted(products, key=lambda p: not p["in_stock"])
    return {"category": key, "products": recommended}


# --- Claude Agent SDK wiring (optional import) -------------------------------
# The import is guarded so the module loads (and the mock fallback works) even
# when claude-agent-sdk / the Claude Code CLI is not installed.

try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
        tool,
    )

    _SDK_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "no SDK"
    _SDK_AVAILABLE = False


if _SDK_AVAILABLE:

    @tool("get_order", "Look up a customer order by its order id.", {"order_id": str})
    async def get_order(args: dict) -> dict:
        result = _lookup_order(args["order_id"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "create_return",
        "Create a product return for an existing order given a reason.",
        {"order_id": str, "reason": str},
    )
    async def create_return(args: dict) -> dict:
        result = _make_return(args["order_id"], args["reason"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "recommend_products",
        "Recommend products from a catalog category (e.g. 'coffee', 'accessories').",
        {"category": str},
    )
    async def recommend_products(args: dict) -> dict:
        result = _list_recommendations(args["category"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    # An in-process MCP server exposes the tools to the SDK's tool loop.
    _MCP_SERVER = create_sdk_mcp_server(
        name="customer-service",
        version="1.0.0",
        tools=[get_order, create_return, recommend_products],
    )


async def _run_agent_sdk(message: str) -> str:
    """Drive the real Claude Agent SDK tool loop and return the final text."""
    # Tool names are namespaced as mcp__<server-name>__<tool-name>.
    allowed_tools = [
        "mcp__customer-service__get_order",
        "mcp__customer-service__create_return",
        "mcp__customer-service__recommend_products",
    ]
    options = ClaudeAgentOptions(
        model=_MODEL_ID,
        system_prompt=_SYSTEM_PROMPT,
        mcp_servers={"customer-service": _MCP_SERVER},
        allowed_tools=allowed_tools,
        # No filesystem/bash tools are needed for this service.
        max_turns=8,
    )

    final_text = ""
    async for msg in query(prompt=message, options=options):
        # Collect assistant text blocks. The SDK yields message objects whose
        # `content` is a list of blocks; text blocks carry a `.text` attribute.
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    final_text = text if not final_text else f"{final_text}\n{text}"
    return final_text.strip()


def _run_agent_mock(message: str) -> str:
    """Deterministic fallback that still exercises the mock tools directly.

    Uses very small keyword heuristics to decide which tool to call so the REST
    API remains useful when the real SDK path is unavailable. This is NOT an
    LLM; it is a demo stand-in.
    """
    lowered = message.lower()

    # Extract an order id token like ORD-1001 if present.
    order_id = None
    for token in message.replace(",", " ").split():
        cleaned = token.strip(".!?()")
        if cleaned.upper().startswith("ORD-"):
            order_id = cleaned.upper()
            break

    if "return" in lowered and order_id:
        result = _make_return(order_id, reason=message)
        if "error" in result:
            return f"[mock] Could not create return: {result['error']}"
        return (
            f"[mock] Return {result['return_id']} created for order "
            f"{result['order_id']} (status: {result['status']})."
        )

    if order_id:
        result = _lookup_order(order_id)
        if "error" in result:
            return f"[mock] {result['error']}."
        return (
            f"[mock] Order {result['order_id']} for {result['customer']} is "
            f"'{result['status']}'. Total ${result['total']:.2f}."
        )

    if "recommend" in lowered or "suggest" in lowered or "product" in lowered:
        category = "accessories" if "accessor" in lowered else "coffee"
        result = _list_recommendations(category)
        names = ", ".join(
            f"{p['name']} (${p['price']:.2f}{'' if p['in_stock'] else ', out of stock'})"
            for p in result["products"]
        )
        return f"[mock] Recommended {result['category']} products: {names}."

    return (
        "[mock] I can help with order status (e.g. 'Where is ORD-1001?'), "
        "returns ('Return ORD-1002, wrong size'), and product recommendations "
        "('Recommend coffee')."
    )


async def run_chat(message: str) -> str:
    """Run a single-turn conversation and return the final assistant text.

    Chooses the real Claude Agent SDK path by default; falls back to the
    deterministic mock when the SDK is unavailable or USE_MOCK_AGENT=true.
    """
    if _USE_MOCK_AGENT or not _SDK_AVAILABLE:
        return _run_agent_mock(message)

    try:
        return await _run_agent_sdk(message)
    except Exception as exc:  # noqa: BLE001 - never fail the request on SDK errors
        # Defensive fallback: if the live SDK call blows up at runtime (missing
        # CLI, Bedrock permissions, etc.) still return a useful mock reply.
        return f"{_run_agent_mock(message)}\n[note: live agent unavailable: {exc}]"
