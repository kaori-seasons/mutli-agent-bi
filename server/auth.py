"""Cognito access-token verification. No trusted identity comes from request headers/body."""
from dataclasses import dataclass
from functools import lru_cache
import json
import os
import re

import jwt

from .errors import ServiceError


@dataclass(frozen=True)
class Principal:
    subject: str
    sources: frozenset[str]
    admin: bool = False


def source_grants():
    try:
        grants = json.loads(os.getenv("AUTH_SOURCE_GRANTS", "{}"))
        if not isinstance(grants, dict) or any(
            not isinstance(k, str) or not re.fullmatch(r"(?:sub|group):.+", k) or not isinstance(v, list)
            or any(not isinstance(x, str) or not x or x == "*" for x in v)
            for k, v in grants.items()
        ):
            raise ValueError()
        return grants
    except (ValueError, TypeError):
        raise ServiceError("AUTH_CONFIGURATION", "身份授权配置无效", 503) from None


def grants_for(claims):
    # Re-evaluated on downloads too; never cache grants with signing keys.
    grants = source_grants()
    try:
        groups = claims.get("cognito:groups", [])
        if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
            raise ValueError()
        sources = set(grants.get("sub:" + claims["sub"], []))
        for group in groups:
            sources.update(grants.get("group:" + group, []))
        admins = set(filter(None, os.getenv("AUTH_ADMIN_GROUPS", "").split(",")))
        return Principal(claims["sub"], frozenset(sources), bool(admins.intersection(groups)))
    except (ValueError, TypeError, KeyError):
        raise ServiceError("AUTH_CONFIGURATION", "身份授权配置无效", 503) from None


@lru_cache(maxsize=8)
def _jwks(issuer):
    return jwt.PyJWKClient(issuer + "/.well-known/jwks.json", timeout=5,
                           cache_jwk_set=True, lifespan=300, cache_keys=False)


def identity_configuration():
    region = os.getenv("BACK_USER_POOL_REGION", "")
    pool = os.getenv("BACK_USER_POOL_ID", "")
    client = os.getenv("BACK_USER_CLIENT_ID", "")
    if (not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region)
            or not re.fullmatch(re.escape(region) + r"_[A-Za-z0-9]+", pool)
            or not re.fullmatch(r"[A-Za-z0-9]+", client)):
        raise ServiceError("AUTH_CONFIGURATION", "身份认证尚未配置", 503)
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://cognito-idp.{region}.{suffix}/{pool}", client


def authenticate(authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise ServiceError("AUTH_REQUIRED", "请先登录", 401)
    token = authorization[7:]
    if not token or len(token) > 16384 or any(c.isspace() for c in token):
        raise ServiceError("INVALID_TOKEN", "登录凭证无效", 401)
    issuer, client = identity_configuration()
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise jwt.InvalidTokenError()
        key = _jwks(issuer).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=issuer,
                            options={"verify_aud": False,
                                     "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"]})
        if (claims["token_use"] != "access" or claims["client_id"] != client
                or not isinstance(claims["sub"], str) or not claims["sub"]):
            raise jwt.InvalidTokenError()
    except jwt.PyJWKClientConnectionError:
        raise ServiceError("AUTH_UNAVAILABLE", "身份验证服务暂时不可用", 503) from None
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ServiceError("INVALID_TOKEN", "登录已过期或凭证无效", 401) from None
    return grants_for(claims)
