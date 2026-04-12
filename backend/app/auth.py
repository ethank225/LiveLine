"""
Supabase JWT verification.

Frontend signs in via supabase.auth.signInWithOAuth({provider:'google'}),
gets a Supabase access token, and sends it as ``Authorization: Bearer <jwt>``
on every API call. For SSE (EventSource can't set custom headers) the
token is passed as ``?token=<jwt>``.

We verify by calling ``supabase.auth.get_user(token)`` via the service
client. This sidesteps every dashboard-revision-dependent secret
(HS256 secret, JWKS, signing-key IDs) — Supabase itself is the oracle.

Cache policy — two tiers:

    FRESH (≤ 1 h):
        Return the cached user directly. No round-trip to Supabase.

    STALE-WHILE-ERROR:
        Entries never auto-evict from the cache (only pruned when the
        dict grows past 500). On a cache miss we re-verify against
        Supabase. If Supabase RAISES (connection refused, 5xx, timeout),
        we serve the stale cached user instead of 401ing. A genuine auth
        failure (Supabase returns a valid response saying "no user")
        still 401s the request — we only swallow transport-level errors.

This means an active user whose token was recently verified keeps
working through a Supabase outage. A fresh login against a down
Supabase still fails, which is correct.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Query, Request, status

from app import database as db

logger = logging.getLogger(__name__)

_FRESH_TTL_SECONDS = 3600.0            # 1 hour — within a Supabase JWT's own lifetime
_MAX_CACHE_ENTRIES = 500                # prune point

_cache: dict[str, tuple["CurrentUser", float]] = {}  # token -> (user, verified_at_monotonic)
_cache_lock = Lock()


@dataclass
class CurrentUser:
    id: str
    email: str | None


def _extract_token(request: Request, token_q: str | None) -> str | None:
    """Pull the JWT from Authorization header or ?token= query param."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    if token_q:
        return token_q
    return None


def _store(token: str, user: "CurrentUser", now: float) -> None:
    with _cache_lock:
        _cache[token] = (user, now)
        if len(_cache) > _MAX_CACHE_ENTRIES:
            # Drop the oldest entries. Approximate LRU — sort once per prune.
            by_age = sorted(_cache.items(), key=lambda kv: kv[1][1], reverse=True)
            _cache.clear()
            _cache.update(dict(by_age[: _MAX_CACHE_ENTRIES // 2]))


def _peek(token: str) -> tuple["CurrentUser", float] | None:
    with _cache_lock:
        return _cache.get(token)


def _verify(token: str) -> CurrentUser:
    now = time.monotonic()

    # Fresh hit — no network.
    hit = _peek(token)
    if hit and (now - hit[1]) < _FRESH_TTL_SECONDS:
        return hit[0]

    client = db._get_client()
    if client is None:
        # No Supabase client at all (misconfigured env). If we happen to
        # have a cached user for this token from a previous healthy
        # period, serve it. Otherwise hard-503.
        if hit is not None:
            logger.warning("JWT verify: Supabase client unavailable — serving stale cache")
            return hit[0]
        logger.error("JWT verify: Supabase client unavailable and no cache")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth backend unavailable")

    try:
        resp = client.auth.get_user(token)
    except Exception as e:
        # Transport-level failure — connection refused, timeout, 5xx.
        # Serve stale if we have any prior verification of this token.
        if hit is not None:
            logger.warning(
                f"JWT verify: transient error, serving stale cache "
                f"({type(e).__name__}: {e})"
            )
            return hit[0]
        logger.warning(f"JWT verify failed: {type(e).__name__}: {e}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")

    # Successful response from Supabase — it's the authority on validity.
    user = getattr(resp, "user", None)
    if not user or not getattr(user, "id", None):
        # Genuine auth rejection (e.g. expired / revoked). Do NOT serve
        # stale — Supabase has spoken.
        logger.warning(f"JWT verify: response had no user: {resp!r}")
        with _cache_lock:
            _cache.pop(token, None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has no user")

    cu = CurrentUser(id=str(user.id), email=getattr(user, "email", None))
    _store(token, cu, now)
    return cu


def get_current_user(
    request: Request,
    token: str | None = Query(default=None),
) -> CurrentUser:
    """FastAPI dependency. Use with: user=Depends(get_current_user)."""
    jwt_str = _extract_token(request, token)
    if not jwt_str:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Bearer token")
    return _verify(jwt_str)


def get_current_user_optional(
    request: Request,
    token: str | None = Query(default=None),
) -> CurrentUser | None:
    """Variant that returns None instead of 401 when no token is present."""
    jwt_str = _extract_token(request, token)
    if not jwt_str:
        return None
    try:
        return _verify(jwt_str)
    except HTTPException:
        return None


__all__ = [
    "CurrentUser",
    "get_current_user",
    "get_current_user_optional",
]
