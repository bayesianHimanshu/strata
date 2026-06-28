"""Auth dependency. When auth_enabled, verify an Entra (Azure AD) OIDC bearer token and
derive the tenant. Locally (auth disabled) a dev principal is injected so the service is
usable without an IdP. The production path validates the JWT signature against the Entra
JWKS for the configured audience/issuer.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from strata_platform.config import Settings, get_settings


@dataclass
class Principal:
    subject: str
    tenant_id: str


def get_principal(authorization: str | None = Header(default=None),
                  settings: Settings = Depends(get_settings)) -> Principal:
    if not settings.auth_enabled:
        return Principal(subject="dev", tenant_id="default")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    claims = _verify_entra_jwt(token, settings)
    return Principal(subject=claims.get("sub", "unknown"),
                     tenant_id=claims.get("tid", "default"))


def _verify_entra_jwt(token: str, settings: Settings) -> dict:  # pragma: no cover
    """Validate signature against Entra JWKS + audience/issuer. Requires PyJWT + the
    tenant's discovery doc; wired in the Azure deployment."""
    import jwt
    from jwt import PyJWKClient

    issuer = settings.entra_issuer or (
        f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0")
    jwks = PyJWKClient(f"{issuer}/discovery/v2.0/keys")
    key = jwks.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["RS256"],
                      audience=settings.entra_api_audience, issuer=issuer)
