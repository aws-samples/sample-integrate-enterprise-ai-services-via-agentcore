#!/usr/bin/env bash
# Render a k8s manifest by substituting ${...} placeholders from the current
# environment, then print the result to stdout.
#
# Usage: scripts/render-k8s.sh <path-to-manifest.yaml>
#
# TLS handling: the ingress manifests contain ${ALB_LISTEN_PORTS} and
# ${ALB_CERT_ANNOTATION}. This script derives them from ACM_CERTIFICATE_ARN:
#   - cert set   -> HTTPS(443) listener + certificate-arn annotation
#   - cert empty -> HTTP(80) only, no certificate annotation
set -euo pipefail

manifest="${1:?usage: render-k8s.sh <manifest.yaml>}"

if [[ -n "${ACM_CERTIFICATE_ARN:-}" ]]; then
  export ALB_LISTEN_PORTS='[{"HTTP":80},{"HTTPS":443}]'
  # Leading spaces keep the annotation correctly indented under annotations:.
  export ALB_CERT_ANNOTATION="    alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERTIFICATE_ARN}
    alb.ingress.kubernetes.io/ssl-redirect: '443'"
else
  export ALB_LISTEN_PORTS='[{"HTTP":80}]'
  export ALB_CERT_ANNOTATION=""
fi

# envsubst only replaces variables we name, so stray $ in manifests are safe.
# Enumerate every placeholder the manifests use.
vars='$K8S_NAMESPACE $SERVICE_ACCOUNT_NAME $ECR_REGISTRY $ECR_REPO_LANGGRAPH'
vars+=' $ECR_REPO_CLAUDE $ECR_REPO_MCP $IMAGE_TAG $AWS_REGION $BEDROCK_MODEL_ID'
vars+=' $COGNITO_USER_POOL_ID $COGNITO_RESOURCE_CLIENT_ID $ALB_LISTEN_PORTS $ALB_CERT_ANNOTATION'

envsubst "$vars" < "$manifest"
