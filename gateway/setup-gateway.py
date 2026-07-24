#!/usr/bin/env python3
"""Create the AgentCore Gateway and its four targets.

Reads gateway-config.yaml (with ${...} substituted from the environment) and
drives the bedrock-agentcore-control API to build:

  Gateway (MCP protocol, CUSTOM_JWT inbound auth via Cognito)
    +-- OAuth2 credential provider (Cognito, outbound to Services 1/2/3)
    +-- API-key credential provider (Jira, outbound to Service 4)
    +-- Target 1: langgraph-agent  -> OpenAPI -> MCP (schema from S3)
    +-- Target 2: claude-agent     -> OpenAPI -> MCP (schema from S3)
    +-- Target 3: mcp-server       -> native MCP server
    +-- Target 4: jira-cloud       -> OpenAPI -> MCP (schema from S3), API key

Note: an aggregating MCP gateway (protocolType=MCP) exposes every target as MCP
tools and therefore requires each target to carry a tool schema. HTTP
passthrough targets have no schema and attach only to a no-protocol gateway, so
Services 1 and 4 are wired as OpenAPI targets here (their operationIds become
the MCP tool names).

The script is idempotent: existing gateway / providers / targets with the same
names are reused instead of recreated. It prints the gateway MCP endpoint URL at
the end for use in Amazon Quick / Kiro.

Secrets (Cognito client secret, Jira token) are read from the environment or
Secrets Manager and passed straight to the API; they are never logged.

Env vars consumed (populated by deploy.sh from config.env + deploy-outputs.env):
  AWS_REGION, PROJECT_NAME, GATEWAY_NAME, GATEWAY_MCP_VERSION, GATEWAY_SEARCH_TYPE,
  GATEWAY_ROLE_ARN,
  COGNITO_DISCOVERY_URL, COGNITO_GATEWAY_CLIENT_ID, COGNITO_SERVICE_CLIENT_ID,
  COGNITO_SERVICE_CLIENT_SECRET, COGNITO_SCOPE,
  LANGGRAPH_ENDPOINT, MCP_ENDPOINT, CLAUDE_OPENAPI_S3_URI,
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN_SECRET_NAME
"""

from __future__ import annotations

import base64
import json
import os
import string
import sys
import time
from pathlib import Path

import boto3
import yaml

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
CONFIG_PATH = Path(__file__).with_name("gateway-config.yaml")
OUTPUT_PATH = Path(__file__).with_name("generated-gateway.json")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[setup-gateway] {msg}", flush=True)


def load_config() -> dict:
    """Load YAML and substitute ${VAR} from the environment."""
    raw = CONFIG_PATH.read_text()
    # string.Template uses ${VAR}; leave unknown vars as empty to catch later.
    template = string.Template(raw)
    substituted = template.safe_substitute(os.environ)
    return yaml.safe_load(substituted)


def require(value: str | None, name: str) -> str:
    if not value:
        log(f"ERROR: required value '{name}' is empty. Did deploy.sh run fully?")
        sys.exit(1)
    return value


# --------------------------------------------------------------------------- #
# identity (credential providers)
# --------------------------------------------------------------------------- #

def get_or_create_oauth_provider(client, cfg: dict) -> str:
    name = require(cfg["name"], "cognito_oauth.name")
    existing = _find_credential_provider(client, "oauth2", name)
    if existing:
        log(f"OAuth2 credential provider '{name}' already exists")
        return existing

    client_secret = require(
        os.environ.get("COGNITO_SERVICE_CLIENT_SECRET"), "COGNITO_SERVICE_CLIENT_SECRET"
    )
    log(f"Creating OAuth2 credential provider '{name}'")
    resp = client.create_oauth2_credential_provider(
        name=name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {"discoveryUrl": require(cfg["discovery_url"], "discovery_url")},
                "clientId": require(cfg["client_id"], "client_id"),
                "clientSecret": client_secret,
            }
        },
    )
    return resp["credentialProviderArn"]


def get_or_create_apikey_provider(client, cfg: dict) -> str | None:
    """Create the Jira Basic-auth credential provider.

    Jira Cloud uses Basic auth: base64(email:api_token). We fetch the API token
    from Secrets Manager, build the base64 credential, and store it as the API
    key. The gateway then sends `Authorization: Basic <base64>`.
    """
    jira_base = os.environ.get("JIRA_BASE_URL", "")
    if not jira_base:
        log("JIRA_BASE_URL is empty - skipping Jira credential provider")
        return None

    name = require(cfg["name"], "jira_api_key.name")
    existing = _find_credential_provider(client, "apikey", name)
    if existing:
        log(f"API-key credential provider '{name}' already exists")
        return existing

    email = require(os.environ.get("JIRA_EMAIL"), "JIRA_EMAIL")
    token = _read_jira_token()
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()

    log(f"Creating API-key credential provider '{name}'")
    resp = client.create_api_key_credential_provider(name=name, apiKey=basic)
    return resp["credentialProviderArn"]


def _read_jira_token() -> str:
    """Return the Jira API token.

    Resolution order:
      1. JIRA_API_TOKEN in the environment (set in the git-ignored config.env) -
         convenient for a quick demo.
      2. AWS Secrets Manager at JIRA_API_TOKEN_SECRET_NAME - the recommended
         path (nothing sensitive on disk).
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
    resp = sm.get_secret_value(SecretId=secret_name)
    raw = resp.get("SecretString", "")
    # Support either a raw token or a JSON blob {"api_token": "..."}.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("api_token") or parsed.get("token") or raw
    except json.JSONDecodeError:
        pass
    return raw


def _find_credential_provider(client, kind: str, name: str) -> str | None:
    """Return the ARN of an existing credential provider by name, or None."""
    try:
        if kind == "oauth2":
            paginator = client.get_paginator("list_oauth2_credential_providers")
        else:
            paginator = client.get_paginator("list_api_key_credential_providers")
    except Exception:  # noqa: BLE001 - paginator may not exist in older botocore
        return None
    for page in paginator.paginate():
        for item in page.get("credentialProviders", []):
            if item.get("name") == name:
                return item.get("credentialProviderArn")
    return None


# --------------------------------------------------------------------------- #
# gateway
# --------------------------------------------------------------------------- #

def get_or_create_gateway(client, cfg: dict) -> dict:
    gw = cfg["gateway"]
    name = require(gw["name"], "gateway.name")

    for page in client.get_paginator("list_gateways").paginate():
        for item in page.get("items", []):
            if item.get("name") == name:
                log(f"Gateway '{name}' already exists")
                return client.get_gateway(gatewayIdentifier=item["gatewayId"])

    role_arn = require(os.environ.get("GATEWAY_ROLE_ARN"), "GATEWAY_ROLE_ARN")
    mcp_config: dict = {"supportedVersions": gw["mcp_supported_versions"]}
    if gw.get("search_type"):
        mcp_config["searchType"] = gw["search_type"]

    inbound = gw["inbound_auth"]
    log(f"Creating gateway '{name}'")
    resp = client.create_gateway(
        name=name,
        description=gw.get("description", ""),
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration={"mcp": mcp_config},
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": require(inbound["discovery_url"], "inbound discovery_url"),
                "allowedClients": inbound["allowed_clients"],
            }
        },
    )
    gateway_id = resp["gatewayId"]
    _wait_gateway_ready(client, gateway_id)
    return client.get_gateway(gatewayIdentifier=gateway_id)


def _wait_gateway_ready(client, gateway_id: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status == "READY":
            return
        if status in ("FAILED", "CREATE_FAILED"):
            log(f"ERROR: gateway entered status {status}")
            sys.exit(1)
        time.sleep(5)
    log("ERROR: timed out waiting for gateway READY")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #

def _oauth_cred_config(provider_arn: str, scopes: list[str]) -> list[dict]:
    return [
        {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {"providerArn": provider_arn, "scopes": scopes}
            },
        }
    ]


def _apikey_cred_config(provider_arn: str, target_cfg: dict) -> list[dict]:
    apikey: dict = {
        "providerArn": provider_arn,
        "credentialLocation": target_cfg.get("api_key_location", "HEADER"),
        "credentialParameterName": target_cfg.get("api_key_parameter_name", "Authorization"),
    }
    prefix = target_cfg.get("api_key_prefix")
    if prefix:
        apikey["credentialPrefix"] = prefix.rstrip()
    return [{"credentialProviderType": "API_KEY", "credentialProvider": {"apiKeyCredentialProvider": apikey}}]


def _target_configuration(target: dict) -> dict:
    kind = target["kind"]
    if kind == "http_passthrough":
        return {
            "http": {
                "passthrough": {
                    "endpoint": require(target["endpoint"], f"{target['name']} endpoint"),
                    "protocolType": "CUSTOM",
                }
            }
        }
    if kind == "openapi":
        return {
            "mcp": {
                "openApiSchema": {
                    "s3": {"uri": require(target["openapi_s3_uri"], f"{target['name']} openapi_s3_uri")}
                }
            }
        }
    if kind == "mcp_server":
        return {
            "mcp": {"mcpServer": {"endpoint": require(target["endpoint"], f"{target['name']} endpoint")}}
        }
    raise ValueError(f"unknown target kind: {kind}")


def create_targets(client, gateway_id: str, cfg: dict, oauth_arn: str, apikey_arn: str | None) -> list[dict]:
    existing = {}
    for page in client.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id):
        for item in page.get("items", []):
            existing[item.get("name")] = item.get("targetId")

    scopes = [os.environ["COGNITO_SCOPE"]] if os.environ.get("COGNITO_SCOPE") else []
    results = []
    for target in cfg["targets"]:
        name = target["name"]

        # Skip the Jira target entirely if it has no schema / credential. Jira is
        # now an OpenAPI target, so its presence is gated on the OpenAPI S3 URI
        # (written by deploy.sh only when JIRA_BASE_URL is configured).
        if target["outbound"] == "jira_api_key" and (
            apikey_arn is None or not target.get("openapi_s3_uri")
        ):
            log(f"Skipping target '{name}' (Jira not configured)")
            continue

        if name in existing:
            log(f"Target '{name}' already exists")
            results.append({"name": name, "targetId": existing[name], "status": "EXISTS"})
            continue

        if target["outbound"] == "jira_api_key":
            cred = _apikey_cred_config(apikey_arn, target)
        else:
            cred = _oauth_cred_config(oauth_arn, scopes)

        log(f"Creating target '{name}' ({target['kind']})")
        resp = client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            targetConfiguration=_target_configuration(target),
            credentialProviderConfigurations=cred,
        )
        target_id = resp["targetId"]
        _wait_target_ready(client, gateway_id, target_id)
        results.append({"name": name, "targetId": target_id, "status": "READY"})
    return results


def _wait_target_ready(client, gateway_id: str, target_id: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"]
        if status == "READY":
            return
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL", "CREATE_FAILED"):
            log(f"WARNING: target {target_id} status {status}")
            return
        time.sleep(5)
    log(f"WARNING: timed out waiting for target {target_id}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    cfg = load_config()
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    oauth_arn = get_or_create_oauth_provider(client, cfg["credential_providers"]["cognito_oauth"])
    apikey_arn = get_or_create_apikey_provider(client, cfg["credential_providers"]["jira_api_key"])

    gateway = get_or_create_gateway(client, cfg)
    gateway_id = gateway["gatewayId"]
    gateway_url = gateway.get("gatewayUrl") or gateway.get("gatewayEndpoint", "")

    targets = create_targets(client, gateway_id, cfg, oauth_arn, apikey_arn)

    output = {
        "gatewayId": gateway_id,
        "gatewayUrl": gateway_url,
        "region": REGION,
        "targets": targets,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))

    log("=" * 60)
    log(f"Gateway ready: {gateway_id}")
    log(f"MCP endpoint : {gateway_url}")
    log(f"Targets      : {', '.join(t['name'] for t in targets)}")
    log(f"Details written to {OUTPUT_PATH.name}")
    log("Configure Amazon Quick / Kiro to connect to the MCP endpoint above.")
    log("=" * 60)


if __name__ == "__main__":
    main()
