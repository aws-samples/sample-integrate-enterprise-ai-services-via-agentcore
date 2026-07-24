#!/usr/bin/env python3
"""Delete the per-service Amazon API Gateway HTTP APIs created at deploy time.

These APIs are built by setup-apigateway.py (not by CDK), so they must be
removed before `cdk destroy` tears down the VPC Link they attach to. Deleting an
API removes its routes, integrations, authorizers, and stages. Best-effort and
idempotent: missing APIs are ignored.
"""

import os
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
PROJECT = os.environ.get("PROJECT_NAME", "agentcore-gw-demo")
API_NAMES = {
    f"{PROJECT}-langgraph-agent-api",
    f"{PROJECT}-claude-agent-api",
    f"{PROJECT}-mcp-server-api",
}


def log(msg: str) -> None:
    print(f"[teardown-apigateway] {msg}", flush=True)


def main() -> int:
    api = boto3.client("apigatewayv2", region_name=REGION)
    deleted = 0
    for page in api.get_paginator("get_apis").paginate():
        for item in page.get("Items", []):
            if item.get("Name") in API_NAMES:
                log(f"Deleting HTTP API {item['Name']} ({item['ApiId']})")
                api.delete_api(ApiId=item["ApiId"])
                deleted += 1
    if not deleted:
        log("No matching HTTP APIs found (already gone)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
