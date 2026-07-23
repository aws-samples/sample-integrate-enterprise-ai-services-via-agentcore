"""LangGraph ReAct agent backed by Amazon Bedrock (Claude).

The agent is created once at import time and reused across requests. It uses the
prebuilt ReAct graph from langgraph, wired to the mock tools in tools.py.
"""

from __future__ import annotations

import os

from langchain_aws import ChatBedrockConverse
from langgraph.prebuilt import create_react_agent

from .tools import ALL_TOOLS

_SYSTEM_PROMPT = (
    "You are a helpful enterprise assistant. Use the available tools to answer "
    "questions about weather, arithmetic, internal knowledge, and the current time. "
    "Be concise. If a tool is not relevant, answer directly."
)


def _build_agent():
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    model_id = os.environ.get(
        "BEDROCK_MODEL_ID", "apac.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    llm = ChatBedrockConverse(
        model=model_id,
        region_name=region,
        temperature=0,
        max_tokens=1024,
    )
    return create_react_agent(llm, ALL_TOOLS, prompt=_SYSTEM_PROMPT)


# Module-level singleton. Import side effect is intentional and cheap.
_AGENT = None


def get_agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = _build_agent()
    return _AGENT


def run_chat(message: str) -> str:
    """Run a single-turn conversation and return the final assistant text."""
    agent = get_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": message}]})
    messages = result.get("messages", [])
    if not messages:
        return ""
    # The last message is the assistant's final answer.
    final = messages[-1]
    content = getattr(final, "content", final)
    if isinstance(content, list):
        # Bedrock Converse can return content blocks; join any text parts.
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)
