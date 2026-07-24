#!/usr/bin/env python3
"""CDK app entry point for the AgentCore Gateway demo infrastructure.

Four stacks, deployed together via `cdk deploy --all`:

  1. EcrStack     - one ECR repository per container service.
  2. BuildStack   - a CodeBuild project + source bucket that build/push the
                    service images on AWS (native amd64), so deploy.sh needs no
                    local Docker.
  3. CognitoStack - User Pool + resource server + app clients for OAuth 2.0
                    (both inbound service auth and outbound gateway auth).
  4. EksStack     - EKS cluster, managed node group, and the AWS Load Balancer
                    Controller (for ALB Ingress). Grants the workload service
                    account permission to invoke Bedrock.

All configuration comes from environment variables sourced from config.env by
deploy.sh. Nothing is hard-coded per environment.
"""

import os

import aws_cdk as cdk

from stacks.build_stack import BuildStack
from stacks.cognito_stack import CognitoStack
from stacks.ecr_stack import EcrStack
from stacks.eks_stack import EksStack


def _env() -> cdk.Environment:
    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_REGION") or os.environ.get("CDK_DEFAULT_REGION") or "ap-northeast-1"
    return cdk.Environment(account=account, region=region)


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


app = cdk.App()
env = _env()

project = _cfg("PROJECT_NAME", "agentcore-gw-demo")

repo_names = [
    _cfg("ECR_REPO_LANGGRAPH", f"{project}/langgraph-agent"),
    _cfg("ECR_REPO_CLAUDE", f"{project}/claude-agent"),
    _cfg("ECR_REPO_MCP", f"{project}/mcp-server"),
]

ecr = EcrStack(
    app,
    f"{project}-ecr",
    env=env,
    repo_names=repo_names,
)

BuildStack(
    app,
    f"{project}-build",
    env=env,
    project=project,
    repo_names=repo_names,
)

cognito = CognitoStack(
    app,
    f"{project}-cognito",
    env=env,
    project=project,
    domain_prefix=_cfg("COGNITO_DOMAIN_PREFIX", project),
)

EksStack(
    app,
    f"{project}-eks",
    env=env,
    project=project,
    cluster_name=_cfg("CLUSTER_NAME", f"{project}-eks"),
    eks_version=_cfg("EKS_VERSION", "1.31"),
    node_instance_type=_cfg("EKS_NODE_INSTANCE_TYPE", "t3.large"),
    node_min=int(_cfg("EKS_NODE_MIN", "2")),
    node_max=int(_cfg("EKS_NODE_MAX", "4")),
    node_desired=int(_cfg("EKS_NODE_DESIRED", "2")),
)

app.synth()
