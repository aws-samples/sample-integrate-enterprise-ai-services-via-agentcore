#!/usr/bin/env bash
# Render a k8s manifest by substituting ${...} placeholders from the current
# environment, then print the result to stdout.
#
# Usage: scripts/render-k8s.sh <path-to-manifest.yaml>
#
# TLS note: the service ALBs are INTERNAL and HTTP-only. TLS is terminated one
# layer up by Amazon API Gateway (execute-api endpoint, AWS-managed cert), so
# there is no ACM certificate or HTTPS listener to inject here.
set -euo pipefail

manifest="${1:?usage: render-k8s.sh <manifest.yaml>}"

# envsubst only replaces variables we name, so stray $ in manifests are safe.
# Enumerate every placeholder the manifests use.
vars='$K8S_NAMESPACE $SERVICE_ACCOUNT_NAME $ECR_REGISTRY $ECR_REPO_LANGGRAPH'
vars+=' $ECR_REPO_CLAUDE $ECR_REPO_MCP $IMAGE_TAG $AWS_REGION $BEDROCK_MODEL_ID'
vars+=' $COGNITO_USER_POOL_ID $COGNITO_RESOURCE_CLIENT_ID $PROJECT_SHORT $AUTH_DISABLED'
vars+=' $MCP_DNS_REBINDING_PROTECTION $MCP_ALLOWED_HOSTS'

envsubst "$vars" < "$manifest"
