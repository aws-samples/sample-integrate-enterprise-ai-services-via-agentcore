#!/usr/bin/env bash
# =============================================================================
# destroy.sh - one-click teardown, reverse order of deploy.sh.
#
#   1. Delete AgentCore Gateway targets + gateway + credential providers
#   2. Delete k8s Ingresses (releases the ALBs) and the services
#   3. Empty + delete the OpenAPI spec S3 bucket
#   4. cdk destroy --all (EKS, Cognito, ECR)
#
# Best-effort: individual failures are logged but do not abort the rest, so a
# partially deployed stack can still be cleaned up.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
# shellcheck disable=SC1091
source ./config.env
[[ -f "$DEPLOY_OUTPUTS" ]] && source "./$DEPLOY_OUTPUTS"
PY="$HERE/venv/bin/python"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi

# --- 1. tear down the gateway ----------------------------------------------
log "Deleting AgentCore Gateway resources"
$PY gateway/teardown-gateway.py || warn "Gateway teardown reported errors (continuing)."

# --- 2. delete k8s resources (frees the ALBs before EKS is destroyed) ------
if command -v kubectl >/dev/null 2>&1 && [[ -n "${CLUSTER_NAME:-}" ]]; then
  log "Deleting k8s services + ingresses"
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
  NS="${K8S_NAMESPACE:-demo}"
  for svc in langgraph-agent claude-agent mcp-server; do
    kubectl -n "$NS" delete ingress "$svc" --ignore-not-found=true 2>/dev/null || true
    kubectl -n "$NS" delete service "$svc" --ignore-not-found=true 2>/dev/null || true
    kubectl -n "$NS" delete deployment "$svc" --ignore-not-found=true 2>/dev/null || true
  done
  log "Waiting 60s for ALBs to deprovision"
  sleep 60
fi

# --- 3. delete the spec bucket ---------------------------------------------
if [[ -n "${AWS_ACCOUNT_ID:-}" ]]; then
  SPEC_BUCKET="${PROJECT_NAME}-gateway-specs-${AWS_ACCOUNT_ID}"
  log "Emptying + deleting S3 bucket ${SPEC_BUCKET}"
  aws s3 rb "s3://${SPEC_BUCKET}" --force 2>/dev/null || warn "Bucket not found or not empty."
fi

# --- 4. cdk destroy ---------------------------------------------------------
log "Destroying CDK stacks (EKS teardown takes ~10-15 min)"
export CDK_DEFAULT_ACCOUNT="${AWS_ACCOUNT_ID:-}"
export CDK_DEFAULT_REGION="$AWS_REGION"
( cd infrastructure/cdk && cdk destroy --all --force --app "$PY app.py" ) \
  || warn "cdk destroy reported errors - check the CloudFormation console."

log "Cleaning up generated files"
rm -f cdk-outputs.json "$DEPLOY_OUTPUTS" gateway/generated-gateway.json

log "Teardown complete."
