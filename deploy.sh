#!/usr/bin/env bash
# =============================================================================
# deploy.sh - one-click deployment of the AgentCore Gateway demo.
#
#   1. Deploy infrastructure (ECR + Cognito + EKS)          via CDK
#   2. Build & push the 3 service container images          via Docker + ECR
#   3. Deploy the services to EKS                            via kubectl
#   4. Wait for ALB endpoints                               via kubectl
#   5. Upload the Claude Agent OpenAPI spec to S3
#   6. Create the AgentCore Gateway + 4 targets             via setup-gateway.py
#
# Prerequisites: aws cli v2, cdk, docker, kubectl, jq, envsubst, python venv.
# Edit config.env before running. Safe to re-run (idempotent where possible).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- 0. load config + venv --------------------------------------------------
# shellcheck disable=SC1091
source ./config.env
PY="$HERE/venv/bin/python"
PIP="$HERE/venv/bin/pip"
[[ -x "$PY" ]] || { echo "venv not found. Run: python3 -m venv venv"; exit 1; }

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

for bin in aws cdk docker kubectl jq envsubst; do
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

[[ -n "$COGNITO_USER_POOL_ID" ]] || die "Could not read Cognito outputs."

# Fetch client secrets WITHOUT printing them.
COGNITO_SERVICE_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$COGNITO_USER_POOL_ID" --client-id "$COGNITO_SERVICE_CLIENT_ID" \
  --region "$AWS_REGION" --query 'UserPoolClient.ClientSecret' --output text)"
COGNITO_GATEWAY_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$COGNITO_USER_POOL_ID" --client-id "$COGNITO_GATEWAY_CLIENT_ID" \
  --region "$AWS_REGION" --query 'UserPoolClient.ClientSecret' --output text)"
export COGNITO_SERVICE_CLIENT_SECRET COGNITO_GATEWAY_CLIENT_SECRET

# --- 3. build & push images -------------------------------------------------
log "Logging in to ECR"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

build_push() {
  local dir="$1" repo="$2"
  log "Building ${repo}"
  docker build --platform linux/amd64 -t "${ECR_REGISTRY}/${repo}:${IMAGE_TAG}" "services/${dir}"
  docker push "${ECR_REGISTRY}/${repo}:${IMAGE_TAG}"
}
build_push langgraph-agent "$ECR_REPO_LANGGRAPH"
build_push claude-agent    "$ECR_REPO_CLAUDE"
build_push mcp-server       "$ECR_REPO_MCP"

# --- 4. deploy to EKS -------------------------------------------------------
log "Updating kubeconfig"
aws eks update-kubeconfig --name "$CLUSTER_NAME_OUT" --region "$AWS_REGION" \
  --role-arn "$KUBECTL_ROLE_ARN"

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

# --- 5. wait for ALB endpoints ---------------------------------------------
wait_alb() {
  local ing="$1" host="" i=0
  while [[ -z "$host" && $i -lt 40 ]]; do
    host="$(kubectl -n "$K8S_NAMESPACE" get ingress "$ing" \
      -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
    [[ -z "$host" ]] && { sleep 15; i=$((i+1)); }
  done
  echo "$host"
}
log "Waiting for ALB provisioning (a few minutes)"
LANGGRAPH_ALB="$(wait_alb langgraph-agent)"
CLAUDE_ALB="$(wait_alb claude-agent)"
MCP_ALB="$(wait_alb mcp-server)"
[[ -n "$LANGGRAPH_ALB" && -n "$CLAUDE_ALB" && -n "$MCP_ALB" ]] \
  || die "ALB endpoints did not become ready."

# Gateway targets require https:// endpoints. Prefer BASE_DOMAIN (with your ACM
# cert + DNS) when provided; otherwise fall back to the raw ALB hostname over
# https (only works if ACM_CERTIFICATE_ARN attached an HTTPS listener).
scheme="https"
if [[ -n "${BASE_DOMAIN}" ]]; then
  export LANGGRAPH_ENDPOINT="${scheme}://langgraph.${BASE_DOMAIN}"
  export CLAUDE_ENDPOINT="${scheme}://claude.${BASE_DOMAIN}"
  export MCP_ENDPOINT="${scheme}://mcp.${BASE_DOMAIN}/mcp"
else
  export LANGGRAPH_ENDPOINT="${scheme}://${LANGGRAPH_ALB}"
  export CLAUDE_ENDPOINT="${scheme}://${CLAUDE_ALB}"
  export MCP_ENDPOINT="${scheme}://${MCP_ALB}/mcp"
fi

# --- 6. upload Claude OpenAPI spec to S3 -----------------------------------
log "Uploading Claude Agent OpenAPI spec to S3"
SPEC_BUCKET="${PROJECT_NAME}-gateway-specs-${AWS_ACCOUNT_ID}"
aws s3api head-bucket --bucket "$SPEC_BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$SPEC_BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
# Inject the resolved host + Cognito domain into the spec before upload.
# The spec uses ${CLAUDE_AGENT_HOST} (host only, no scheme) and ${COGNITO_DOMAIN}.
CLAUDE_HOST="${CLAUDE_ENDPOINT#https://}"; CLAUDE_HOST="${CLAUDE_HOST%%/*}"
COGNITO_DOMAIN_HOST="${COGNITO_TOKEN_URL#https://}"; COGNITO_DOMAIN_HOST="${COGNITO_DOMAIN_HOST%%/*}"
tmp_spec="$(mktemp --suffix=.yaml)"
sed "s|\${CLAUDE_AGENT_HOST}|${CLAUDE_HOST}|g; s|\${COGNITO_DOMAIN}|${COGNITO_DOMAIN_HOST}|g" \
  services/claude-agent/openapi.yaml > "$tmp_spec"
aws s3 cp "$tmp_spec" "s3://${SPEC_BUCKET}/claude-agent/openapi.yaml" >/dev/null
rm -f "$tmp_spec"
export CLAUDE_OPENAPI_S3_URI="s3://${SPEC_BUCKET}/claude-agent/openapi.yaml"

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
