#!/usr/bin/env bash
# =============================================================================
# deploy.sh - one-click deployment of the AgentCore Gateway demo.
#
#   1. Deploy infrastructure (ECR + CodeBuild + Cognito + EKS + VPC Link) via CDK
#   2. Build & push the 3 service container images            via AWS CodeBuild
#   3. Deploy the services to EKS (internal ALBs)             via kubectl
#   4. Wait for the internal ALBs                             via kubectl
#   5. Front each service with an API Gateway HTTP API        via setup-apigateway.py
#   6. Upload the OpenAPI specs (langgraph, claude, jira) to S3
#   7. Create the AgentCore Gateway + 4 targets               via setup-gateway.py
#
# TLS: each service is reached through its API Gateway execute-api endpoint
# (AWS-managed certificate) - no custom domain or ACM certificate required.
#
# Prerequisites: aws cli v2, cdk, kubectl, jq, envsubst, zip, python venv.
# NOTE: no local Docker needed - images build on AWS CodeBuild.
# Copy config.example.env to config.env and edit it before running. Safe to
# re-run (idempotent where possible).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- 0. load config + venv --------------------------------------------------
if [[ ! -f ./config.env ]]; then
  echo "config.env not found. Create it first:" >&2
  echo "    cp config.example.env config.env   # then edit config.env" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ./config.env
PY="$HERE/venv/bin/python"
PIP="$HERE/venv/bin/pip"
[[ -x "$PY" ]] || { echo "venv not found. Run: python3 -m venv venv"; exit 1; }

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

for bin in aws cdk kubectl jq envsubst zip; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' is required but not installed."
done

if [[ -z "${AWS_ACCOUNT_ID}" ]]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  export AWS_ACCOUNT_ID
fi
export CDK_DEFAULT_ACCOUNT="$AWS_ACCOUNT_ID"
export CDK_DEFAULT_REGION="$AWS_REGION"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export ECR_REGISTRY

log "Account ${AWS_ACCOUNT_ID}, region ${AWS_REGION}"

# --- 1. install deps + deploy CDK ------------------------------------------
log "Installing Python dependencies (CDK + gateway)"
$PIP install --quiet -r infrastructure/cdk/requirements.txt
$PIP install --quiet -r gateway/requirements.txt

log "Bootstrapping CDK environment (idempotent)"
cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}" \
  --app "$PY infrastructure/cdk/app.py" >/dev/null 2>&1 || true

log "Deploying infrastructure stacks (EKS provisioning takes ~15-25 min)"
( cd infrastructure/cdk && cdk deploy --all --require-approval never \
    --app "$PY app.py" \
    --outputs-file "$HERE/cdk-outputs.json" )

# --- 2. read CDK outputs ----------------------------------------------------
log "Reading stack outputs"
out() { jq -r --arg s "$1" --arg k "$2" '.[$s][$k] // empty' cdk-outputs.json; }

COGNITO_STACK="${PROJECT_NAME}-cognito"
EKS_STACK="${PROJECT_NAME}-eks"

export COGNITO_USER_POOL_ID="$(out "$COGNITO_STACK" UserPoolId)"
export COGNITO_DISCOVERY_URL="$(out "$COGNITO_STACK" CognitoDiscoveryUrl)"
export COGNITO_TOKEN_URL="$(out "$COGNITO_STACK" CognitoTokenUrl)"
export COGNITO_SCOPE="$(out "$COGNITO_STACK" CognitoScope)"
export COGNITO_SERVICE_CLIENT_ID="$(out "$COGNITO_STACK" ServiceClientId)"
export COGNITO_GATEWAY_CLIENT_ID="$(out "$COGNITO_STACK" GatewayClientId)"
# The services validate that inbound tokens come from the service client.
export COGNITO_RESOURCE_CLIENT_ID="$COGNITO_SERVICE_CLIENT_ID"
export CLUSTER_NAME_OUT="$(out "$EKS_STACK" ClusterName)"
export KUBECTL_ROLE_ARN="$(out "$EKS_STACK" KubectlRoleArn)"
export K8S_NAMESPACE="$(out "$EKS_STACK" WorkloadNamespace)"
export SERVICE_ACCOUNT_NAME="$(out "$EKS_STACK" WorkloadServiceAccount)"
export VPC_LINK_ID="$(out "$EKS_STACK" VpcLinkId)"
BUILD_STACK="${PROJECT_NAME}-build"
export BUILD_PROJECT="$(out "$BUILD_STACK" BuildProjectName)"
export BUILD_SOURCE_BUCKET="$(out "$BUILD_STACK" BuildSourceBucket)"

[[ -n "$COGNITO_USER_POOL_ID" ]] || die "Could not read Cognito outputs."

# Fetch client secrets WITHOUT printing them.
COGNITO_SERVICE_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$COGNITO_USER_POOL_ID" --client-id "$COGNITO_SERVICE_CLIENT_ID" \
  --region "$AWS_REGION" --query 'UserPoolClient.ClientSecret' --output text)"
COGNITO_GATEWAY_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$COGNITO_USER_POOL_ID" --client-id "$COGNITO_GATEWAY_CLIENT_ID" \
  --region "$AWS_REGION" --query 'UserPoolClient.ClientSecret' --output text)"
export COGNITO_SERVICE_CLIENT_SECRET COGNITO_GATEWAY_CLIENT_SECRET

# --- 3. build & push images (on AWS CodeBuild, no local Docker) -------------
# Each service directory is zipped, uploaded to the CodeBuild source bucket, and
# built on CodeBuild (native linux/amd64). This removes any local Docker / cross
# architecture emulation requirement. Builds run sequentially and the script
# waits for each to reach SUCCEEDED.
[[ -n "$BUILD_PROJECT" && -n "$BUILD_SOURCE_BUCKET" ]] \
  || die "Could not read CodeBuild outputs (BuildProjectName / BuildSourceBucket)."

build_on_codebuild() {
  local dir="$1" repo="$2" key="sources/${dir}.zip" zip
  log "Building ${repo} on CodeBuild"
  # zip refuses to append to the empty file mktemp leaves behind, so use a plain
  # path and remove any stale copy first. Exclude caches / VCS from the context.
  zip="${TMPDIR:-/tmp}/acgw-build-${dir}.zip"
  rm -f "$zip"
  # Zip the service dir contents at the archive root so the Dockerfile is at ./.
  ( cd "services/${dir}" && zip -qr "$zip" . -x '*/__pycache__/*' '__pycache__/*' '*.pyc' '.git/*' )
  aws s3 cp "$zip" "s3://${BUILD_SOURCE_BUCKET}/${key}" >/dev/null
  rm -f "$zip"

  local build_id
  build_id="$(aws codebuild start-build \
    --project-name "$BUILD_PROJECT" \
    --region "$AWS_REGION" \
    --source-location-override "${BUILD_SOURCE_BUCKET}/${key}" \
    --source-type-override S3 \
    --environment-variables-override \
      "name=REPO,value=${repo},type=PLAINTEXT" \
      "name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT" \
    --query 'build.id' --output text)"
  echo "  build id: ${build_id}"

  # Poll until the build finishes.
  local status="IN_PROGRESS"
  while [[ "$status" == "IN_PROGRESS" ]]; do
    sleep 15
    status="$(aws codebuild batch-get-builds --ids "$build_id" --region "$AWS_REGION" \
      --query 'builds[0].buildStatus' --output text)"
    echo "  ${repo}: ${status}"
  done
  [[ "$status" == "SUCCEEDED" ]] \
    || die "CodeBuild for ${repo} ended with status ${status}. See the CodeBuild console logs for build ${build_id}."
}
build_on_codebuild langgraph-agent "$ECR_REPO_LANGGRAPH"
build_on_codebuild claude-agent    "$ECR_REPO_CLAUDE"
build_on_codebuild mcp-server      "$ECR_REPO_MCP"

# --- 4. deploy to EKS -------------------------------------------------------
log "Updating kubeconfig"
aws eks update-kubeconfig --name "$CLUSTER_NAME_OUT" --region "$AWS_REGION" \
  --role-arn "$KUBECTL_ROLE_ARN"

# mcp >= 1.23 validates the Host header (DNS rebinding protection). The pod's
# real front-door hostnames are only known after step 5 creates the API Gateway,
# so apply with an empty allow-list now (the service falls back to loopback
# defaults) and re-render the Deployment in step 5b with the resolved hosts.
export MCP_DNS_REBINDING_PROTECTION="${MCP_DNS_REBINDING_PROTECTION:-true}"
export MCP_ALLOWED_HOSTS=""

log "Applying k8s manifests"
for svc in langgraph-agent claude-agent mcp-server; do
  for m in deployment service; do
    scripts/render-k8s.sh "services/${svc}/k8s/${m}.yaml" | kubectl apply -f -
  done
done

log "Waiting for deployments to roll out"
for svc in langgraph-agent claude-agent mcp-server; do
  kubectl -n "$K8S_NAMESPACE" rollout status "deploy/${svc}" --timeout=300s
done

# --- 5. wait for the internal ALBs, then front them with API Gateway --------
# The internal ALBs carry no public hostname; instead we wait for each Ingress's
# LB to report a hostname (meaning the LB + listener exist) and then let
# setup-apigateway.py resolve the HTTP:80 listener ARN by ALB name.
wait_alb() {
  local ing="$1" host="" i=0
  while [[ -z "$host" && $i -lt 40 ]]; do
    host="$(kubectl -n "$K8S_NAMESPACE" get ingress "$ing" \
      -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
    [[ -z "$host" ]] && { sleep 15; i=$((i+1)); }
  done
  echo "$host"
}
log "Waiting for internal ALB provisioning (a few minutes)"
for svc in langgraph-agent claude-agent mcp-server; do
  [[ -n "$(wait_alb "$svc")" ]] || die "Internal ALB for '$svc' did not become ready."
done

log "Creating API Gateway HTTP APIs (execute-api HTTPS front doors)"
$PY gateway/setup-apigateway.py

# Read the resolved execute-api endpoints. These are the https:// URLs the
# AgentCore Gateway targets connect to (AWS-managed cert, no custom domain).
apigw() { jq -r --arg s "$1" '.[$s].endpoint // empty' gateway/generated-apigateway.json; }
export LANGGRAPH_ENDPOINT="$(apigw langgraph-agent)"
export CLAUDE_ENDPOINT="$(apigw claude-agent)"
export MCP_ENDPOINT="$(apigw mcp-server)"
[[ -n "$LANGGRAPH_ENDPOINT" && -n "$CLAUDE_ENDPOINT" && -n "$MCP_ENDPOINT" ]] \
  || die "API Gateway endpoints were not written."

# --- 5b. re-apply the MCP Deployment with its Host allow-list ---------------
# mcp >= 1.23 rejects requests whose Host header is not allow-listed (HTTP 421,
# GHSA-9h52-p55h-vw2f). Allow both hostnames that can appear on the hop into the
# pod: the API Gateway execute-api host and the internal ALB host (whichever
# layer sets Host, the request is accepted). ":*" permits any port.
MCP_APIGW_HOST="${MCP_ENDPOINT#https://}"; MCP_APIGW_HOST="${MCP_APIGW_HOST%%/*}"
MCP_ALB_HOST="$(kubectl -n "$K8S_NAMESPACE" get ingress mcp-server \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
MCP_ALLOWED_HOSTS="${MCP_APIGW_HOST},${MCP_APIGW_HOST}:*"
[[ -n "$MCP_ALB_HOST" ]] && MCP_ALLOWED_HOSTS+=",${MCP_ALB_HOST},${MCP_ALB_HOST}:*"
export MCP_ALLOWED_HOSTS

log "Re-applying mcp-server with Host allow-list (${MCP_ALLOWED_HOSTS})"
scripts/render-k8s.sh services/mcp-server/k8s/deployment.yaml | kubectl apply -f -
kubectl -n "$K8S_NAMESPACE" rollout status deploy/mcp-server --timeout=300s

# --- 6. upload OpenAPI specs to S3 -----------------------------------------
# An aggregating MCP gateway exposes each target as MCP tools, so every target
# needs a tool schema. Services 1 and 2 ship OpenAPI specs; the gateway turns
# each operation into an MCP tool. Jira (Service 4) ships a spec too when
# configured. The specs use ${..._HOST} (host only, no scheme) and
# ${COGNITO_DOMAIN} placeholders, substituted here with resolved values.
log "Uploading OpenAPI specs to S3"
SPEC_BUCKET="${PROJECT_NAME}-gateway-specs-${AWS_ACCOUNT_ID}"
aws s3api head-bucket --bucket "$SPEC_BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$SPEC_BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null

COGNITO_DOMAIN_HOST="${COGNITO_TOKEN_URL#https://}"; COGNITO_DOMAIN_HOST="${COGNITO_DOMAIN_HOST%%/*}"

# upload_spec <src-yaml> <s3-key> <host-placeholder> <host-value>
upload_spec() {
  local src="$1" key="$2" ph="$3" host="$4" tmp
  tmp="$(mktemp --suffix=.yaml)"
  sed "s|\${${ph}}|${host}|g; s|\${COGNITO_DOMAIN}|${COGNITO_DOMAIN_HOST}|g" \
    "$src" > "$tmp"
  aws s3 cp "$tmp" "s3://${SPEC_BUCKET}/${key}" >/dev/null
  rm -f "$tmp"
  echo "s3://${SPEC_BUCKET}/${key}"
}

LANGGRAPH_HOST="${LANGGRAPH_ENDPOINT#https://}"; LANGGRAPH_HOST="${LANGGRAPH_HOST%%/*}"
CLAUDE_HOST="${CLAUDE_ENDPOINT#https://}"; CLAUDE_HOST="${CLAUDE_HOST%%/*}"
export LANGGRAPH_OPENAPI_S3_URI="$(upload_spec services/langgraph-agent/openapi.yaml \
  langgraph-agent/openapi.yaml LANGGRAPH_AGENT_HOST "$LANGGRAPH_HOST")"
export CLAUDE_OPENAPI_S3_URI="$(upload_spec services/claude-agent/openapi.yaml \
  claude-agent/openapi.yaml CLAUDE_AGENT_HOST "$CLAUDE_HOST")"

# Jira (Service 4) is external SaaS; only wire it when JIRA_BASE_URL is set. Its
# spec server URL is the Jira site itself (not an execute-api endpoint).
if [[ -n "${JIRA_BASE_URL}" ]]; then
  # Ensure the demo's Jira project exists (idempotent) so Quick agents always
  # target a valid software project instead of, e.g., a discovery project whose
  # only issue type is "Idea".
  log "Ensuring Jira project '${JIRA_PROJECT_KEY:-ACGW}' exists"
  $PY gateway/setup-jira-project.py

  JIRA_HOST="${JIRA_BASE_URL#https://}"; JIRA_HOST="${JIRA_HOST%%/*}"
  # The Jira subset hard-codes its server URL; substitute the site host in place
  # of the your-domain.atlassian.net placeholder line.
  tmp_jira="$(mktemp --suffix=.yaml)"
  sed "s|https://your-domain.atlassian.net|${JIRA_BASE_URL%/}|g" \
    services/jira-integration/jira-openapi-subset.yaml > "$tmp_jira"
  aws s3 cp "$tmp_jira" "s3://${SPEC_BUCKET}/jira-cloud/openapi.yaml" >/dev/null
  rm -f "$tmp_jira"
  export JIRA_OPENAPI_S3_URI="s3://${SPEC_BUCKET}/jira-cloud/openapi.yaml"
fi

# --- 7. create the gateway + targets ---------------------------------------
export GATEWAY_ROLE_ARN="$(out "$COGNITO_STACK" GatewayRoleArn)"
[[ -n "$GATEWAY_ROLE_ARN" ]] || die "Could not read GatewayRoleArn output."

log "Creating AgentCore Gateway + targets"
$PY gateway/setup-gateway.py

# --- 8. persist outputs -----------------------------------------------------
log "Writing ${DEPLOY_OUTPUTS}"
GATEWAY_URL="$(jq -r '.gatewayUrl // empty' gateway/generated-gateway.json 2>/dev/null || true)"
cat > "$DEPLOY_OUTPUTS" <<EOF
# Generated by deploy.sh - resolved endpoints and identifiers.
export AWS_REGION="${AWS_REGION}"
export AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}"
export COGNITO_USER_POOL_ID="${COGNITO_USER_POOL_ID}"
export COGNITO_DISCOVERY_URL="${COGNITO_DISCOVERY_URL}"
export COGNITO_TOKEN_URL="${COGNITO_TOKEN_URL}"
export COGNITO_SCOPE="${COGNITO_SCOPE}"
export COGNITO_SERVICE_CLIENT_ID="${COGNITO_SERVICE_CLIENT_ID}"
export COGNITO_GATEWAY_CLIENT_ID="${COGNITO_GATEWAY_CLIENT_ID}"
export LANGGRAPH_ENDPOINT="${LANGGRAPH_ENDPOINT}"
export CLAUDE_ENDPOINT="${CLAUDE_ENDPOINT}"
export MCP_ENDPOINT="${MCP_ENDPOINT}"
export CLAUDE_OPENAPI_S3_URI="${CLAUDE_OPENAPI_S3_URI}"
export GATEWAY_URL="${GATEWAY_URL}"
EOF

log "DONE"
echo "Gateway MCP endpoint: ${GATEWAY_URL:-<see gateway/generated-gateway.json>}"
echo "Service endpoints written to ${DEPLOY_OUTPUTS}"
echo "Validate with: source ${DEPLOY_OUTPUTS} && venv/bin/python gateway/validate.py"
