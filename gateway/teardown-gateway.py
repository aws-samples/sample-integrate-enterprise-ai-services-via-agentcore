#!/usr/bin/env python3
"""Delete the AgentCore Gateway, its targets, and the demo credential providers.

Best-effort and idempotent: missing resources are ignored. Names are resolved
from the same environment variables setup-gateway.py uses.
"""

from __future__ import annotations

import os
import time

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
PROJECT = os.environ.get("PROJECT_NAME", "agentcore-gw-demo")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", f"{PROJECT}-gateway")


def log(msg: str) -> None:
    print(f"[teardown] {msg}", flush=True)


def main() -> None:
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Find the gateway by name.
    gateway_id = None
    try:
        for page in client.get_paginator("list_gateways").paginate():
            for item in page.get("items", []):
                if item.get("name") == GATEWAY_NAME:
                    gateway_id = item["gatewayId"]
    except Exception as exc:  # noqa: BLE001
        log(f"Could not list gateways: {exc}")

    if gateway_id:
        # Delete targets first.
        try:
            for page in client.get_paginator("list_gateway_targets").paginate(
                gatewayIdentifier=gateway_id
            ):
                for t in page.get("items", []):
                    log(f"Deleting target {t.get('name')}")
                    client.delete_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=t["targetId"]
                    )
        except Exception as exc:  # noqa: BLE001
            log(f"Target deletion error: {exc}")
        time.sleep(5)
        log(f"Deleting gateway {GATEWAY_NAME}")
        try:
            client.delete_gateway(gatewayIdentifier=gateway_id)
        except Exception as exc:  # noqa: BLE001
            log(f"Gateway deletion error: {exc}")
    else:
        log(f"Gateway '{GATEWAY_NAME}' not found (already gone)")

    # Delete credential providers by name.
    _delete_provider(client, "oauth2", f"{PROJECT}-cognito-oauth")
    _delete_provider(client, "apikey", f"{PROJECT}-jira-apikey")


def _delete_provider(client, kind: str, name: str) -> None:
    try:
        if kind == "oauth2":
            paginator = client.get_paginator("list_oauth2_credential_providers")
            deleter = client.delete_oauth2_credential_provider
        else:
            paginator = client.get_paginator("list_api_key_credential_providers")
            deleter = client.delete_api_key_credential_provider
        for page in paginator.paginate():
            for item in page.get("credentialProviders", []):
                if item.get("name") == name:
                    log(f"Deleting credential provider {name}")
                    deleter(name=name)
                    return
        log(f"Credential provider '{name}' not found")
    except Exception as exc:  # noqa: BLE001
        log(f"Credential provider deletion error ({name}): {exc}")


if __name__ == "__main__":
    main()
