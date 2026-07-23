# Integrate Enterprise AI Services with Amazon Bedrock AgentCore Gateway

A **one-click deployable demo** showing how self-hosted enterprise AI services
connect to AWS AI agents (Amazon Quick, Kiro, and others) through **Amazon
Bedrock AgentCore Gateway**.

The demo stands up four representative "enterprise AI service" shapes, then puts
a single AgentCore Gateway in front of them so any MCP client sees one unified,
OAuth-protected tool catalog.

```
                          Amazon EKS Cluster (ap-northeast-1)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Service 1              Service 2              Service 3           │
   │  LangGraph Agent        Claude Agent SDK       MCP Server          │
   │  bespoke REST + OAuth    OpenAPI 3.0 + OAuth    Streamable HTTP     │
   │  (HTTP passthrough)      (OpenAPI → MCP)        (native MCP)        │
   │       │                       │                     │              │
   │       └────── one ALB per service (HTTPS, Cognito OAuth) ──────────│
   └──────────────────────────────┬───────────────────────────────────┘
                                   │
                    ┌──────────────┴───────────────┐
                    │      AgentCore Gateway         │
                    │  T1 HTTP Passthrough (CUSTOM)  │  ← Service 1
                    │  T2 OpenAPI → MCP              │  ← Service 2
                    │  T3 MCP Server                 │  ← Service 3
                    │  T4 HTTP Passthrough (CUSTOM)  │  ← Jira Cloud (SaaS)
                    └──────────────┬───────────────┘
                                   │  (MCP, inbound OAuth/JWT)
                    ┌──────────────┴───────────────┐
                    │   Amazon Quick / Kiro (MCP)    │
                    └────────────────────────────────┘
```

## What you get

| Part | Contents |
|------|----------|
| **4 sample services** | Real, runnable code + Dockerfiles + k8s manifests. Services 1–3 deploy to EKS; Service 4 (Jira) is an external SaaS target, no deployment. |
| **One-click deploy/destroy** | `deploy.sh` / `destroy.sh` cover the full lifecycle: CDK infra → images → k8s → gateway. |
| **Gateway automation** | `gateway/setup-gateway.py` creates the gateway, credential providers, and all 4 targets via `bedrock-agentcore-control`. |
| **Manual guide** | `docs/console-setup-guide.md` — step-by-step AWS Console instructions with screenshot placeholders. |

### The four services

1. **LangGraph Agent** (`services/langgraph-agent`) — a ReAct agent (calculator,
   weather, knowledge, time tools) behind a **non-standard REST API**
   (`/api/v1/chat`, `/api/v1/tasks`). No OpenAPI spec → wired as an **HTTP
   Passthrough (CUSTOM)** target. Runs Claude on **real Bedrock**.
2. **Claude Agent SDK** (`services/claude-agent`) — a customer-service agent
   (orders, returns, recommendations) with a **complete OpenAPI 3.0 spec**
   (`openapi.yaml`) → AgentCore Gateway auto-converts it to MCP tools. Uses the
   Anthropic **Claude Agent SDK** against Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`).
3. **MCP Server** (`services/mcp-server`) — an enterprise knowledge-base
   **native MCP server** (Streamable HTTP, `/mcp`) with 4 tools → wired as an
   **MCP Server** target.
4. **Jira Cloud** (`services/jira-integration`) — external SaaS, configured as an
   **HTTP Passthrough (CUSTOM)** target with **API-key** (Basic auth) outbound.

All services enforce **Cognito OAuth 2.0** inbound; the gateway authenticates
outbound with a Cognito OAuth credential provider (services 1–3) or an API-key
provider (Jira).

## Prerequisites

- AWS account with permissions for EKS, ECR, Cognito, IAM, S3, Secrets Manager,
  and Bedrock AgentCore, plus **Bedrock model access** enabled for the Claude
  model in `config.env` (region `ap-northeast-1`).
- Local tools: **AWS CLI v2**, **AWS CDK v2** (`npm i -g aws-cdk`), **Docker**,
  **kubectl**, **jq**, **envsubst** (gettext), **Python 3.10+**.
- The included **`venv/`** Python environment (created on first setup — see
  below). It is git-ignored.

## Quick start

```bash
# 1. Create the Python environment (if not already present)
python3 -m venv venv
./venv/bin/pip install -r infrastructure/cdk/requirements.txt -r gateway/requirements.txt

# 2. Edit configuration (region defaults to ap-northeast-1)
$EDITOR config.env          # set Bedrock model, optionally Jira + TLS values

# 3. Deploy everything (EKS provisioning takes ~15-25 min)
./deploy.sh

# 4. Validate gateway connectivity + tool discovery
source deploy-outputs.env
./venv/bin/python gateway/validate.py

# 5. Tear everything down
./destroy.sh
```

`deploy.sh` writes resolved endpoints and identifiers to `deploy-outputs.env`
(git-ignored) and gateway details to `gateway/generated-gateway.json`.

## TLS is required for gateway targets

AgentCore Gateway targets **must be `https://` endpoints**. Two options:

- **Recommended:** set `ACM_CERTIFICATE_ARN` and `BASE_DOMAIN` in `config.env`.
  Each service's ALB gets an HTTPS listener; after deploy, create DNS records
  (`langgraph.`, `claude.`, `mcp.` under your domain) pointing at the ALBs.
- **Without a cert:** the ALBs serve HTTP only — usable for local `curl` smoke
  tests, but the gateway targets cannot connect. Provide a cert for a real run.

## Configuration

Everything environment-specific lives in **`config.env`** — region, naming, EKS
sizing, Bedrock model id, Cognito domain, gateway options, TLS, and Jira. No
endpoints, ARNs, or secrets are hard-coded anywhere else. Secrets (Cognito
client secrets, the Jira API token) are read at deploy time from Cognito /
Secrets Manager and never written to disk or logs.

## Repository layout

```
sample-integrate-enterprise-ai-services-with-agentcore/
├── README.md
├── config.env                     # single source of truth
├── deploy.sh / destroy.sh         # one-click lifecycle
├── infrastructure/cdk/            # CDK (Python): ECR, Cognito, EKS stacks
├── services/
│   ├── langgraph-agent/           # Service 1
│   ├── claude-agent/              # Service 2 (+ openapi.yaml)
│   ├── mcp-server/                # Service 3
│   └── jira-integration/          # Service 4 config + Jira OpenAPI subset
├── gateway/
│   ├── gateway-config.yaml        # declarative gateway + targets
│   ├── setup-gateway.py           # create gateway + 4 targets
│   ├── teardown-gateway.py        # delete gateway resources
│   └── validate.py                # connectivity + tool-discovery check
├── scripts/render-k8s.sh          # ${...} placeholder substitution for k8s
└── docs/console-setup-guide.md    # manual AWS Console walkthrough
```

## How the pieces connect

1. **CDK** provisions ECR repos, a Cognito user pool (resource server +
   service/gateway app clients + gateway execution role), and an EKS cluster
   with the AWS Load Balancer Controller and a Bedrock-enabled IRSA account.
2. **deploy.sh** builds/pushes the three images, renders and applies the k8s
   manifests, waits for the ALBs, uploads the Claude OpenAPI spec to S3, and
   runs **setup-gateway.py**.
3. **setup-gateway.py** creates the outbound credential providers, the gateway
   (MCP protocol, Cognito JWT inbound auth), and the four targets, then reports
   the gateway MCP endpoint URL.
4. Point **Amazon Quick / Kiro** at that MCP endpoint (bearer token from the
   gateway app client) to use every service's tools through one connection.

For the manual Console path, see **[docs/console-setup-guide.md](docs/console-setup-guide.md)**.

## Cost note

EKS (control plane + 2× `t3.large` nodes), 4 ALBs, NAT gateway, and real Bedrock
inference all incur charges. Run `./destroy.sh` when finished. This is a demo,
not a production reference.

## Security

- OAuth 2.0 end-to-end (Cognito) for inbound and outbound auth; no hard-coded
  credentials.
- Secrets live in Cognito / AWS Secrets Manager and are resolved at runtime.
- Pods run as non-root; Bedrock access is granted via IRSA, not static keys.
- See `SECURITY` notes inline in `deploy.sh` and each service's `auth.py`.
