"""Oura OAuth 2.0 helpers.

Oura's flow is plain OAuth2 authorization-code (no PKCE) over two endpoints —
no vendor SDK needed, so this module speaks httpx directly. The public API is:

* :func:`build_authorization_url` — produce the consent URL for the web-callback flow.
* :func:`exchange_code` — server-side code → token exchange.
* :func:`ingest_tokens` — persist tokens obtained externally (e.g. a mobile app that
  did the OAuth dance and POSTs the resulting token dict to your backend).
* :func:`refresh_access_token` — refresh a stored connection's tokens (Oura
  refresh tokens are single-use and rotate on every refresh).
* :func:`revoke` — revoke the access token at Oura and mark the connection
  revoked.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from django.conf import settings

from .constants import (
    API_BASE_URL,
    DEFAULT_SCOPES,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
    USERCOLLECTION_PATH,
)
from .models import ConnectionStatus, OuraConnection
from .schemas import OAuthFlowState, OuraTokens

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """Base for OAuth-related errors raised by this module."""


class StateMismatchError(OAuthError):
    """Raised when the ``state`` returned from Oura doesn't match what we stashed."""


class TokenExchangeError(OAuthError):
    """Raised when the token endpoint returns a non-2xx response."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"token endpoint returned HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _configured_scopes() -> list[str]:
    return list(getattr(settings, "OURA_SCOPES", DEFAULT_SCOPES))


def build_authorization_url(
    *, scopes: list[str] | None = None, state: str | None = None
) -> tuple[str, OAuthFlowState]:
    """Build the consent URL and the state to round-trip via the session.

    ``scopes`` defaults to ``settings.OURA_SCOPES`` (falling back to
    :data:`~oura.constants.DEFAULT_SCOPES`). Oura users consent per-scope on
    the authorization page, so the granted set may be narrower than requested;
    the actually-granted scopes come back on the token response.
    """
    flow_state = OAuthFlowState(state=state or secrets.token_urlsafe(32))
    params = {
        "response_type": "code",
        "client_id": settings.OURA_CLIENT_ID,
        "redirect_uri": settings.OURA_REDIRECT_URI,
        "scope": " ".join(scopes if scopes is not None else _configured_scopes()),
        "state": flow_state.state,
    }
    return f"{OAUTH_AUTHORIZATION_URL}?{urlencode(params)}", flow_state


def _token_request(data: dict[str, str]) -> OuraTokens:
    response = httpx.post(
        OAUTH_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise TokenExchangeError(response.status_code, response.text)
    return OuraTokens.model_validate(response.json())


def exchange_code(
    *,
    code: str,
    expected_state: str | None = None,
    received_state: str | None = None,
) -> OuraTokens:
    """Exchange an authorization code for tokens.

    Pass ``expected_state`` and ``received_state`` to enforce CSRF protection at
    this layer; pass neither to skip (e.g. when the upstream view already
    validated).
    """
    if expected_state is not None and received_state != expected_state:
        raise StateMismatchError("OAuth state mismatch")
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.OURA_CLIENT_ID,
            "client_secret": settings.OURA_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.OURA_REDIRECT_URI,
        }
    )


def ingest_tokens(
    *,
    customer: AbstractBaseUser,
    tokens: OuraTokens | dict[str, Any],
    oura_user_id: str | None = None,
    now: datetime | None = None,
) -> OuraConnection:
    """Persist tokens onto an :class:`OuraConnection` (create or update).

    This is the entry point for the "mobile app already did the OAuth dance
    and is shipping us the token dict" pattern. ``oura_user_id`` is resolved
    via ``GET personal_info`` if not provided (its ``id`` field is readable
    with any token, no scope needed). Best-effort — a transient API failure
    stores an empty value rather than failing the connect.
    """
    parsed = (
        tokens if isinstance(tokens, OuraTokens) else OuraTokens.model_validate(tokens)
    )
    if oura_user_id is None:
        try:
            oura_user_id = _fetch_oura_user_id(parsed.access_token)
        except (httpx.HTTPError, OAuthError) as exc:
            # The OAuth flow itself succeeded; the user id is only needed for
            # webhook routing. Store empty and let a later sync resolve it.
            log.warning(
                "personal_info fetch failed (%s) — storing empty oura_user_id", exc
            )
            oura_user_id = ""

    connection, _ = OuraConnection.objects.update_or_create(
        customer=customer,
        defaults={
            "oura_user_id": oura_user_id,
            "access_token": parsed.access_token,
            "refresh_token": parsed.refresh_token,
            "token_expires_at": parsed.expires_at(now=now),
            "scopes": parsed.scopes,
            "status": ConnectionStatus.ACTIVE,
        },
    )
    return connection


def refresh_access_token(connection: OuraConnection) -> OuraConnection:
    """Refresh the connection's tokens in place using its stored refresh token.

    Oura refresh tokens are single-use: the grant response carries a NEW
    refresh token and the old one is invalidated, so both tokens persist
    together.
    """
    tokens = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.OURA_CLIENT_ID,
            "client_secret": settings.OURA_CLIENT_SECRET,
            "refresh_token": connection.refresh_token,
        }
    )
    connection.access_token = tokens.access_token
    connection.refresh_token = tokens.refresh_token
    connection.token_expires_at = tokens.expires_at()
    connection.save(
        update_fields=[
            "access_token",
            "refresh_token",
            "token_expires_at",
        ]
    )
    return connection


def revoke(connection: OuraConnection) -> None:
    """Revoke the access token at Oura and mark the connection ``REVOKED``.

    Best-effort: a non-2xx from Oura still flips the local status — the
    user-facing intent (disconnect) shouldn't be blocked by a transient error.
    """
    try:
        httpx.post(
            OAUTH_REVOKE_URL,
            params={"access_token": connection.access_token},
            timeout=10.0,
        )
    except httpx.HTTPError:
        pass
    connection.status = ConnectionStatus.REVOKED
    connection.save(update_fields=["status"])


def _fetch_oura_user_id(access_token: str) -> str:
    """Call ``GET personal_info`` to resolve the stable Oura user id for a token."""
    response = httpx.get(
        f"{API_BASE_URL}/{USERCOLLECTION_PATH}/personal_info",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if response.status_code >= 400:
        # raise_for_status drops the response body; we want it visible.
        raise OAuthError(
            f"personal_info returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    user_id = payload.get("id")
    if not user_id:
        raise OAuthError(f"personal_info returned no user id: {payload!r}")
    return str(user_id)
