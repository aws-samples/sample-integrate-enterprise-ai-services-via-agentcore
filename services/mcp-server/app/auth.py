"""Cognito OAuth 2.0 JWT validation (inbound auth).

Validates the `Authorization: Bearer <token>` header on incoming requests
against the service's Cognito User Pool. The signing keys (JWKS) are fetched
once and cached. Access tokens issued via the Client Credentials grant are
validated for signature, expiry, issuer, and token_use.

This is the auth layer that AgentCore Gateway satisfies on the OUTBOUND side
via its Cognito OAuth credential provider.

NOTE: This module is copied verbatim from Service 1 (langgraph-agent) so the
Cognito validation logic, env vars and JWKS caching stay identical across the
fleet. The only difference is that this MCP server has no FastAPI dependency,
so the request/exception primitives are imported from Starlette (FastAPI's
Request/HTTPException are thin subclasses of these). `require_auth` is applied
as ASGI middleware on the `/mcp` mount (see main.py) to satisfy the MCP
2025-06-18 OAuth 2.1 resource-server expectations.
"""

from __future__ import annotations

import os
import time

import httpx
from jose import jwt
from jose.exceptions import JWTError
from starlette import status
from starlette.exceptions import HTTPException
from starlette.requests import Request

_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
# Optional: restrict to a specific app client id (the resource server client).
_EXPECTED_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "")
# When true (local dev only) auth is bypassed. NEVER set in production.
_AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "false").lower() == "true"

_ISSUER = f"https://cognito-idp.{_REGION}.amazonaws.com/{_USER_POOL_ID}"
_JWKS_URL = f"{_ISSUER}/.well-known/jwks.json"

# JWKS is rotated rarely; cache with a soft TTL.
_JWKS_CACHE: dict[str, object] = {"keys": None, "fetched_at": 0.0}
_JWKS_TTL_SECONDS = 3600


def _get_jwks() -> dict:
    now = time.time()
    if _JWKS_CACHE["keys"] is None or now - float(_JWKS_CACHE["fetched_at"]) > _JWKS_TTL_SECONDS:
        resp = httpx.get(_JWKS_URL, timeout=5.0)
        resp.raise_for_status()
        _JWKS_CACHE["keys"] = resp.json()
        _JWKS_CACHE["fetched_at"] = now
    return _JWKS_CACHE["keys"]  # type: ignore[return-value]


def _verify_token(token: str) -> dict:
    try:
        headers = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Malformed token: {exc}")

    kid = headers.get("kid")
    jwks = _get_jwks()
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        # Key may have rotated; force a refresh once.
        _JWKS_CACHE["keys"] = None
        jwks = _get_jwks()
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown signing key")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=_ISSUER,
            # Client-credentials access tokens have no 'aud'; audience is checked
            # via client_id below instead.
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}")

    if claims.get("token_use") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Expected an access token")

    if _EXPECTED_CLIENT_ID and claims.get("client_id") != _EXPECTED_CLIENT_ID:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unauthorized client")

    return claims


async def require_auth(request: Request) -> dict:
    """Enforce a valid Cognito access token on the incoming request."""
    if _AUTH_DISABLED:
        return {"sub": "local-dev", "token_use": "access"}

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.split(" ", 1)[1].strip()
    return _verify_token(token)
