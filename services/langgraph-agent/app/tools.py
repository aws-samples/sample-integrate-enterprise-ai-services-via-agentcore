"""Example tools for the LangGraph ReAct agent.

These are intentionally self-contained mocks - no external data sources - so the
demo runs anywhere without extra credentials or network access.
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

from langchain_core.tools import tool

# --- calculator -------------------------------------------------------------

# Whitelisted operators for the safe arithmetic evaluator. We deliberately do
# NOT use eval(); we walk a parsed AST and only allow arithmetic nodes.
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):  # numbers
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numeric constants are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


@tool
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '3 * (4 + 5)'.

    Supports + - * / // % ** and parentheses only.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree.body)
        return f"{expression} = {result}"
    except Exception as exc:  # noqa: BLE001 - surface a friendly message to the agent
        return f"Could not evaluate '{expression}': {exc}"


# --- weather (mock) ---------------------------------------------------------

_MOCK_WEATHER = {
    "tokyo": ("Sunny", 27),
    "osaka": ("Cloudy", 25),
    "seattle": ("Rainy", 14),
    "san francisco": ("Foggy", 17),
    "singapore": ("Thunderstorms", 30),
}


@tool
def get_weather(city: str) -> str:
    """Return the current (mock) weather for a city."""
    key = city.strip().lower()
    if key in _MOCK_WEATHER:
        condition, temp_c = _MOCK_WEATHER[key]
        return f"Weather in {city.title()}: {condition}, {temp_c}C."
    return f"No weather data for '{city}'. Known cities: {', '.join(sorted(_MOCK_WEATHER))}."


# --- knowledge retrieval (mock) --------------------------------------------

_MOCK_KB = {
    "refund policy": "Refunds are processed within 5-7 business days of approval.",
    "shipping": "Standard shipping takes 3-5 business days; express takes 1-2.",
    "warranty": "All products carry a 12-month limited warranty from purchase date.",
    "support hours": "Support is available Mon-Fri, 09:00-18:00 JST.",
}


@tool
def search_knowledge(query: str) -> str:
    """Search the internal knowledge base for a topic and return a summary."""
    q = query.strip().lower()
    hits = [f"- {k}: {v}" for k, v in _MOCK_KB.items() if any(w in k for w in q.split())]
    if hits:
        return "Knowledge base results:\n" + "\n".join(hits)
    return f"No knowledge base entries matched '{query}'. Topics: {', '.join(_MOCK_KB)}."


@tool
def current_time() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


ALL_TOOLS = [calculator, get_weather, search_knowledge, current_time]
