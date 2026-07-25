#!/usr/bin/env python3
"""Idempotently create the Jira project the demo steers all tool calls at.

Quick agents fail confusingly when they aim Jira tool calls at the wrong
project: a discovery project (like the default "MDP") only accepts the "Idea"
issue type, so createIssue with issuetype=Task returns an HTTP 400 that the
gateway masks as a generic "internal error". To avoid that, deploy.sh calls
this script to ensure a known software project (JIRA_PROJECT_KEY, default ACGW)
exists up front - a team-managed kanban project whose issue types include Task,
Story, Bug, Epic. The Jira OpenAPI tool descriptions then tell the agent to use
this key and to keep JQL bounded.

Idempotent: if the project key already exists it is left untouched.

Env vars consumed (populated by deploy.sh from config.env):
  AWS_REGION,
  JIRA_BASE_URL, JIRA_EMAIL,
  JIRA_API_TOKEN | JIRA_API_TOKEN_SECRET_NAME,
  JIRA_PROJECT_KEY, JIRA_PROJECT_NAME

Nothing sensitive is printed. Exits 0 (no-op) when JIRA_BASE_URL is empty.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import boto3
import httpx

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
# Team-managed ("next-gen") kanban template - gives Task/Story/Bug/Epic and does
# not require a pre-existing shared configuration scheme.
TEMPLATE_KEY = "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban"


def log(msg: str) -> None:
    print(f"[setup-jira-project] {msg}", flush=True)


def require(value: str | None, name: str) -> str:
    if not value:
        log(f"ERROR: required env var {name} is not set")
        sys.exit(1)
    return value


def read_jira_token() -> str:
    """Return the Jira API token (inline env first, then Secrets Manager).

    Mirrors setup-gateway.py so both scripts resolve the token the same way.
    """
    inline = os.environ.get("JIRA_API_TOKEN", "").strip()
    if inline:
        log("Using Jira API token from JIRA_API_TOKEN (config.env)")
        return inline

    secret_name = require(
        os.environ.get("JIRA_API_TOKEN_SECRET_NAME"), "JIRA_API_TOKEN_SECRET_NAME"
    )
    log(f"Reading Jira API token from Secrets Manager ({secret_name})")
    sm = boto3.client("secretsmanager", region_name=REGION)
    raw = sm.get_secret_value(SecretId=secret_name).get("SecretString", "")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("api_token") or parsed.get("token") or raw
    except json.JSONDecodeError:
        pass
    return raw


def main() -> None:
    base_url = os.environ.get("JIRA_BASE_URL", "").strip()
    if not base_url:
        log("JIRA_BASE_URL is empty; skipping Jira project setup.")
        return

    base = base_url.rstrip("/")
    email = require(os.environ.get("JIRA_EMAIL"), "JIRA_EMAIL")
    key = require(os.environ.get("JIRA_PROJECT_KEY"), "JIRA_PROJECT_KEY")
    name = os.environ.get("JIRA_PROJECT_NAME") or f"{key} project"
    token = read_jira_token()

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        # Idempotency: is the project key already present?
        r = client.get(f"/rest/api/3/project/{key}")
        if r.status_code == 200:
            log(f"Jira project '{key}' already exists; leaving it untouched.")
            return
        if r.status_code not in (404,):
            # 401/403 etc. - surface a clear message (without the token).
            log(f"ERROR: could not query project '{key}' (HTTP {r.status_code}): {r.text[:300]}")
            sys.exit(1)

        # Project lead must be an accountId; use the token owner.
        me = client.get("/rest/api/3/myself")
        me.raise_for_status()
        lead = me.json()["accountId"]

        log(f"Creating Jira software project '{key}' ({name})")
        payload = {
            "key": key,
            "name": name,
            "projectTypeKey": "software",
            "projectTemplateKey": TEMPLATE_KEY,
            "leadAccountId": lead,
            "assigneeType": "PROJECT_LEAD",
            "description": "Auto-created by the AgentCore Gateway demo deploy script.",
        }
        cr = client.post("/rest/api/3/project", json=payload)
        if cr.status_code in (200, 201):
            log(f"Created Jira project '{key}'.")
            return
        # A concurrent/re-run create can race to 400 "key already used"; treat a
        # now-existing key as success.
        if cr.status_code == 400 and client.get(f"/rest/api/3/project/{key}").status_code == 200:
            log(f"Jira project '{key}' exists (created concurrently).")
            return
        log(f"ERROR: failed to create project '{key}' (HTTP {cr.status_code}): {cr.text[:400]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
