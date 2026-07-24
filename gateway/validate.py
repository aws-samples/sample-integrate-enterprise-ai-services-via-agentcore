#!/usr/bin/env python3
"""Validate AgentCore Gateway connectivity and tool discovery.

Steps:
  1. Mint an inbound access token from Cognito (gateway app client,
     client_credentials grant).
  2. Call the gateway MCP endpoint: initialize + tools/list.
  3. Print the discovered tools grouped by target.

Run after setup-gateway.py. Reads gateway/generated-gateway.json for the
endpoint and the environment for Cognito details.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

GENERATED = Path(__file__).with_name("generated-gateway.json")


def _token() -> str:
    token_url = os.environ["COGNITO_TOKEN_URL"]
    client_id = os.environ["COGNITO_GATEWAY_CLIENT_ID"]
    client_secret = os.environ["COGNITO_GATEWAY_CLIENT_SECRET"]
    scope = os.environ.get("COGNITO_SCOPE", "")
    resp = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _mcp_call(endpoint: str, token: str, method: str, params: dict | None, rpc_id: int) -> dict:
    # The gateway advertises MCP protocol 2025-06-18 only. Pin it on every
    # request via the MCP-Protocol-Version header, otherwise post-initialize
    # calls default to 2025-03-26 and the gateway rejects them with a 400
    # (-32022 "Unsupported protocol version").
    mcp_version = os.environ.get("GATEWAY_MCP_VERSION", "2025-06-18")
    resp = httpx.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": mcp_version,
        },
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
        timeout=30.0,
    )
    resp.raise_for_status()
    # Streamable HTTP may return SSE; handle both.
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        return {}
    return resp.json()


def main() -> None:
    if not GENERATED.exists():
        print("generated-gateway.json not found. Run setup-gateway.py first.")
        sys.exit(1)
    info = json.loads(GENERATED.read_text())
    endpoint = info.get("gatewayUrl")
    if not endpoint:
        print("No gatewayUrl in generated-gateway.json")
        sys.exit(1)

    print(f"Gateway endpoint: {endpoint}")
    token = _token()
    print("Obtained inbound access token.")

    _mcp_call(endpoint, token, "initialize", {
        "protocolVersion": os.environ.get("GATEWAY_MCP_VERSION", "2025-06-18"),
        "capabilities": {},
        "clientInfo": {"name": "validate.py", "version": "1.0"},
    }, 1)
    print("MCP initialize OK.")

    tools = _mcp_call(endpoint, token, "tools/list", {}, 2)
    tool_list = tools.get("result", {}).get("tools", [])
    print(f"\nDiscovered {len(tool_list)} tools:")
    for t in tool_list:
        print(f"  - {t.get('name')}: {t.get('description', '')[:70]}")

    if not tool_list:
        print("\nWARNING: no tools discovered. Check target sync status.")
        sys.exit(2)
    print("\nValidation succeeded.")


if __name__ == "__main__":
    main()
