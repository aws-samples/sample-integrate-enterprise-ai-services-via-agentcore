#!/usr/bin/env python3
"""Front each self-hosted service with an Amazon API Gateway HTTP API.

Why this exists
---------------
AgentCore Gateway targets must be https:// endpoints with a publicly trusted
certificate. Rather than terminate TLS on the service ALBs (which would need an
ACM certificate and a custom domain), we put an API Gateway HTTP API in front of
each service. Its built-in

    https://{api-id}.execute-api.{region}.amazonaws.com

endpoint carries an AWS-managed, publicly trusted certificate, so no domain and
no ACM certificate are required. API Gateway reaches the INTERNAL service ALBs
privately over a VPC Link (created by the CDK EKS stack).

For each of the three EKS services this script get-or-creates:

  * an HTTP API (name "{PROJECT_NAME}-{service}-api")
  * a JWT authorizer  -> issuer = the demo Cognito user pool,
                         audience = [gateway client id, service client id]
    Cognito client_credentials access tokens carry `client_id` but no `aud`, and
    API Gateway validates `client_id` against the audience list when `aud` is
    absent -- so the gateway's outbound token is accepted.
  * a private integration (HTTP_PROXY, ConnectionType=VPC_LINK) to the ALB's
    HTTP:80 listener ARN
  * a "$default" route requiring the JWT authorizer + the "invoke" scope
  * a "$default" stage with auto-deploy (so the invoke URL has no stage path)

Idempotent: every resource is looked up by name/key and reused if present.
Writes generated-apigateway.json with each service's resolved https endpoint.

Env vars consumed (populated by deploy.sh from config.env + CDK outputs):
  AWS_REGION, PROJECT_NAME,
  COGNITO_USER_POOL_ID, COGNITO_GATEWAY_CLIENT_ID, COGNITO_SERVICE_CLIENT_ID,
  COGNITO_SCOPE, VPC_LINK_ID
"""

import json
import os
import sys
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
PROJECT = os.environ.get("PROJECT_NAME", "agentcore-gw-demo")
PROJECT_SHORT = os.environ.get("PROJECT_SHORT", "acgw-demo")
OUTPUT_PATH = Path(__file__).with_name("generated-apigateway.json")

# service key -> (ALB load-balancer-name from the Ingress annotation, path
# suffix appended to the execute-api base URL to form the gateway target).
SERVICES = {
    "langgraph-agent": {"alb": f"{PROJECT_SHORT}-langgraph", "path": ""},
    "claude-agent": {"alb": f"{PROJECT_SHORT}-claude", "path": ""},
    "mcp-server": {"alb": f"{PROJECT_SHORT}-mcp", "path": "/mcp"},
}


def log(msg: str) -> None:
    print(f"[setup-apigateway] {msg}", flush=True)


def require(value: str | None, name: str) -> str:
    if not value:
        raise SystemExit(f"ERROR: required value '{name}' is empty. Did deploy.sh run fully?")
    return value


# --------------------------------------------------------------------------- #
# ALB listener discovery
# --------------------------------------------------------------------------- #

def find_http_listener_arn(elbv2, alb_name: str) -> str:
    """Return the HTTP:80 listener ARN of the internal ALB named alb_name."""
    try:
        lbs = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"]
    except elbv2.exceptions.LoadBalancerNotFoundException:
        raise SystemExit(
            f"ERROR: internal ALB '{alb_name}' not found. Is the Ingress "
            "provisioned and the LB active?"
        )
    lb_arn = lbs[0]["LoadBalancerArn"]
    listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
    for listener in listeners:
        if listener.get("Port") == 80 and listener.get("Protocol") == "HTTP":
            return listener["ListenerArn"]
    raise SystemExit(f"ERROR: ALB '{alb_name}' has no HTTP:80 listener.")


# --------------------------------------------------------------------------- #
# HTTP API get-or-create helpers
# --------------------------------------------------------------------------- #

def find_api(api, name: str) -> dict | None:
    for page in api.get_paginator("get_apis").paginate():
        for item in page.get("Items", []):
            if item.get("Name") == name:
                return item
    return None


def get_or_create_api(api, name: str) -> dict:
    existing = find_api(api, name)
    if existing:
        log(f"HTTP API '{name}' already exists")
        return existing
    log(f"Creating HTTP API '{name}'")
    return api.create_api(Name=name, ProtocolType="HTTP")


def get_or_create_authorizer(api, api_id: str, issuer: str, audiences: list[str]) -> str:
    name = f"{PROJECT}-cognito-jwt"
    for item in api.get_authorizers(ApiId=api_id).get("Items", []):
        if item.get("Name") == name:
            return item["AuthorizerId"]
    log(f"Creating JWT authorizer on API {api_id}")
    resp = api.create_authorizer(
        ApiId=api_id,
        Name=name,
        AuthorizerType="JWT",
        IdentitySource=["$request.header.Authorization"],
        JwtConfiguration={"Issuer": issuer, "Audience": audiences},
    )
    return resp["AuthorizerId"]


def get_or_create_integration(api, api_id: str, listener_arn: str, vpc_link_id: str) -> str:
    for item in api.get_integrations(ApiId=api_id).get("Items", []):
        if item.get("IntegrationUri") == listener_arn:
            return item["IntegrationId"]
    log(f"Creating VPC_LINK integration on API {api_id}")
    resp = api.create_integration(
        ApiId=api_id,
        IntegrationType="HTTP_PROXY",
        IntegrationMethod="ANY",
        ConnectionType="VPC_LINK",
        ConnectionId=vpc_link_id,
        IntegrationUri=listener_arn,
        PayloadFormatVersion="1.0",
    )
    return resp["IntegrationId"]


def ensure_default_route(api, api_id: str, integration_id: str, authorizer_id: str, scope: str) -> None:
    target = f"integrations/{integration_id}"
    for item in api.get_routes(ApiId=api_id).get("Items", []):
        if item.get("RouteKey") == "$default":
            api.update_route(
                ApiId=api_id,
                RouteId=item["RouteId"],
                Target=target,
                AuthorizationType="JWT",
                AuthorizerId=authorizer_id,
                AuthorizationScopes=[scope],
            )
            return
    log(f"Creating $default route on API {api_id}")
    api.create_route(
        ApiId=api_id,
        RouteKey="$default",
        Target=target,
        AuthorizationType="JWT",
        AuthorizerId=authorizer_id,
        AuthorizationScopes=[scope],
    )


def ensure_default_stage(api, api_id: str) -> None:
    try:
        api.get_stage(ApiId=api_id, StageName="$default")
        return
    except api.exceptions.NotFoundException:
        pass
    log(f"Creating $default auto-deploy stage on API {api_id}")
    api.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_service(api, elbv2, name: str, spec: dict, issuer: str,
                  audiences: list[str], scope: str, vpc_link_id: str) -> dict:
    listener_arn = find_http_listener_arn(elbv2, spec["alb"])
    api_obj = get_or_create_api(api, f"{PROJECT}-{name}-api")
    api_id = api_obj["ApiId"]
    authorizer_id = get_or_create_authorizer(api, api_id, issuer, audiences)
    integration_id = get_or_create_integration(api, api_id, listener_arn, vpc_link_id)
    ensure_default_route(api, api_id, integration_id, authorizer_id, scope)
    ensure_default_stage(api, api_id)
    # ApiEndpoint is the execute-api base URL (auto-managed https, AWS cert).
    base = api_obj.get("ApiEndpoint") or api.get_api(ApiId=api_id)["ApiEndpoint"]
    endpoint = f"{base}{spec['path']}"
    log(f"{name}: {endpoint}")
    return {"name": name, "apiId": api_id, "endpoint": endpoint}


def main() -> int:
    issuer = (
        f"https://cognito-idp.{REGION}.amazonaws.com/"
        f"{require(os.environ.get('COGNITO_USER_POOL_ID'), 'COGNITO_USER_POOL_ID')}"
    )
    # The gateway's OUTBOUND token uses the service client; include the gateway
    # client too so direct calls (manual/console demo path) are also accepted.
    audiences = [
        require(os.environ.get("COGNITO_SERVICE_CLIENT_ID"), "COGNITO_SERVICE_CLIENT_ID"),
        require(os.environ.get("COGNITO_GATEWAY_CLIENT_ID"), "COGNITO_GATEWAY_CLIENT_ID"),
    ]
    scope = require(os.environ.get("COGNITO_SCOPE"), "COGNITO_SCOPE")
    vpc_link_id = require(os.environ.get("VPC_LINK_ID"), "VPC_LINK_ID")

    api = boto3.client("apigatewayv2", region_name=REGION)
    elbv2 = boto3.client("elbv2", region_name=REGION)

    results = {}
    for name, spec in SERVICES.items():
        info = build_service(api, elbv2, name, spec, issuer, audiences, scope, vpc_link_id)
        results[name] = info

    OUTPUT_PATH.write_text(json.dumps(results, indent=2))
    log("=" * 60)
    log(f"Wrote {OUTPUT_PATH.name}")
    for name, info in results.items():
        log(f"  {name}: {info['endpoint']}")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
