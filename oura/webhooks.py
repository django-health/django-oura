"""Oura webhook subscriptions and incoming notification processing.

Webhooks are Oura's recommended architecture: one historical pull at connect
time, then notification-driven fetches. Notifications arrive ~30 seconds
after a user's ring syncs.

Subscription management
-----------------------

Subscriptions are app-level (authenticated with the client id/secret via
``x-client-id`` / ``x-client-secret`` headers, not user tokens) and are
created per ``(event_type, data_type)`` pair. On creation Oura verifies the
callback endpoint with a GET challenge — the receiver view in
:mod:`oura.views` answers it — and the subscription then expires periodically
unless renewed. Manage them with :func:`create_subscription`,
:func:`list_subscriptions`, :func:`renew_subscription`,
:func:`delete_subscription`, or the matching ``manage.py`` commands.

Notification processing
-----------------------

Each notification POST carries one event::

    {"event_type": "update", "data_type": "sleep",
     "object_id": "...", "event_time": "...", "user_id": "..."}

:func:`process_notification` routes the event to a user by Oura's stable
``user_id``, fetches the referenced document with that user's credentials,
and ingests it into healthdatamodel. It is safe to call from a queue worker —
the receiver view only emits the :data:`oura.signals.notification_received`
signal, and your handler decides whether to process inline or hand off.
Oura expects a fast 2xx (under 10 seconds; failures are retried ~10 times).

Signature verification
----------------------

Oura signs notifications with ``x-oura-signature``: uppercase hex
HMAC-SHA256 over ``timestamp + body`` keyed with the client secret, where
``timestamp`` is the ``x-oura-timestamp`` header. :func:`verify_signature`
implements that check against the raw request body.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from django.conf import settings

from .client import OuraClient
from .constants import API_BASE_URL, DATA_TYPE_WORKOUT, WEBHOOK_SUBSCRIPTION_PATH
from .ingest import RECORD_MAPPERS, ingest_documents
from .models import OuraConnection

log = logging.getLogger(__name__)

# Data types we know how to ingest. Notifications for anything else (e.g.
# daily_stress before a mapper exists) are counted but skipped.
KNOWN_DATA_TYPES = frozenset(RECORD_MAPPERS) | {DATA_TYPE_WORKOUT}

_SUBSCRIPTION_URL = f"{API_BASE_URL}/{WEBHOOK_SUBSCRIPTION_PATH}"


class WebhookError(Exception):
    """Raised when the webhook subscription API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str):
        super().__init__(
            f"webhook subscription API returned HTTP {status_code}: {body}"
        )
        self.status_code = status_code
        self.body = body


# Signature verification --------------------------------------------------


def compute_signature(*, timestamp: str, body: bytes, client_secret: str) -> str:
    """Uppercase hex HMAC-SHA256 over ``timestamp + body``, keyed with the secret."""
    mac = hmac.new(
        client_secret.encode("utf-8"),
        timestamp.encode("utf-8") + body,
        hashlib.sha256,
    )
    return mac.hexdigest().upper()


def verify_signature(
    *,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    client_secret: str | None = None,
) -> bool:
    """True if ``signature`` matches the HMAC of ``timestamp + body``."""
    if not signature or timestamp is None:
        return False
    secret = client_secret if client_secret is not None else settings.OURA_CLIENT_SECRET
    expected = compute_signature(timestamp=timestamp, body=body, client_secret=secret)
    return hmac.compare_digest(expected, signature)


# Subscription management ---------------------------------------------------


def _app_headers() -> dict[str, str]:
    return {
        "x-client-id": settings.OURA_CLIENT_ID,
        "x-client-secret": settings.OURA_CLIENT_SECRET,
    }


def _subscription_request(
    method: str, url: str, *, json: dict[str, Any] | None = None
) -> Any:
    response = httpx.request(
        method, url, headers=_app_headers(), json=json, timeout=30.0
    )
    if response.status_code >= 400:
        raise WebhookError(response.status_code, response.text)
    if not response.content:
        return None
    return response.json()


def create_subscription(
    *,
    callback_url: str,
    verification_token: str,
    event_type: str,
    data_type: str,
) -> dict[str, Any]:
    """``POST /v2/webhook/subscription`` — one (event_type, data_type) pair.

    Oura synchronously GETs ``callback_url`` with the verification token and a
    challenge during this call, so the receiver endpoint must already be live
    and reachable when you run this.
    """
    return _subscription_request(
        "POST",
        _SUBSCRIPTION_URL,
        json={
            "callback_url": callback_url,
            "verification_token": verification_token,
            "event_type": event_type,
            "data_type": data_type,
        },
    )


def list_subscriptions() -> list[dict[str, Any]]:
    """``GET /v2/webhook/subscription`` — all active subscriptions for the app."""
    return list(_subscription_request("GET", _SUBSCRIPTION_URL) or [])


def renew_subscription(subscription_id: str) -> dict[str, Any]:
    """``PUT /v2/webhook/subscription/renew/{id}`` — push out ``expiration_time``."""
    return _subscription_request("PUT", f"{_SUBSCRIPTION_URL}/renew/{subscription_id}")


def delete_subscription(subscription_id: str) -> None:
    """``DELETE /v2/webhook/subscription/{id}``."""
    _subscription_request("DELETE", f"{_SUBSCRIPTION_URL}/{subscription_id}")


# Notification processing ----------------------------------------------------


def process_notification(payload: dict[str, Any]) -> int:
    """Fetch + ingest the document referenced by one notification payload.

    Returns the number of records/workouts written. Unknown users, unmapped
    data types, and ``delete`` events are logged and skipped (returning 0),
    never raised — webhook processing should not error on events this version
    doesn't handle.
    """
    event_type = str(payload.get("event_type") or "")
    data_type = str(payload.get("data_type") or "")
    object_id = str(payload.get("object_id") or "")
    user_id = str(payload.get("user_id") or "")

    if event_type == "delete":
        # healthdatamodel has no delete API; tombstones are skipped. (Sleep
        # documents additionally arrive as type="deleted" updates, which the
        # sleep mapper drops.)
        log.info("Skipping delete event for %s %s", data_type, object_id)
        return 0
    if data_type not in KNOWN_DATA_TYPES:
        log.info("Skipping notification for unmapped data_type=%s", data_type)
        return 0
    if not object_id or not user_id:
        log.warning("Notification missing object_id/user_id: %r", payload)
        return 0

    try:
        connection = OuraConnection.objects.get(oura_user_id=user_id)
    except OuraConnection.DoesNotExist:
        log.warning("Notification for unknown oura user_id=%s", user_id)
        return 0

    with OuraClient(connection) as client:
        document = client.get_document(data_type, object_id)

    count = ingest_documents(connection, data_type, [document])
    connection.last_sync_at = datetime.now(timezone.utc)
    connection.save(update_fields=["last_sync_at"])
    return count
