"""HTTP views for OAuth + webhook notifications.

The OAuth views (``connect`` / ``callback`` / ``disconnect``) cover the
web-callback flow used in admin / dev / testing. Mobile clients should POST
tokens (or an auth code) to a project-local endpoint that calls
:func:`oura.oauth.ingest_tokens` or :func:`oura.oauth.exchange_code`.

The ``notification_receiver`` view serves double duty on one URL, matching
how Oura drives a callback endpoint:

* **GET** — the subscription-time verification challenge. Oura sends
  ``verification_token`` and ``challenge`` query params; the view checks the
  token against ``settings.OURA_WEBHOOK_VERIFICATION_TOKEN`` and echoes
  ``{"challenge": ...}``.
* **POST** — an event notification. The view checks the ``x-oura-signature``
  HMAC (see :func:`oura.webhooks.verify_signature`) and emits a
  :data:`oura.signals.notification_received` signal for every verified body.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import oauth, webhooks
from .models import OuraConnection
from .signals import notification_received

SESSION_KEY = "oura_oauth_flow"


def _success_url() -> str:
    return getattr(settings, "OURA_CONNECT_SUCCESS_URL", "/admin/")


@login_required
@require_http_methods(["GET"])
def connect(request: HttpRequest) -> HttpResponse:
    auth_url, flow_state = oauth.build_authorization_url()
    request.session[SESSION_KEY] = flow_state.model_dump()
    return redirect(auth_url)


@login_required
@require_http_methods(["GET"])
def callback(request: HttpRequest) -> HttpResponse:
    code = request.GET.get("code")
    received_state = request.GET.get("state")
    error = request.GET.get("error")
    if error:
        return HttpResponseBadRequest(f"OAuth error: {error}")
    if not code:
        return HttpResponseBadRequest("Missing authorization code")

    stashed = request.session.pop(SESSION_KEY, None)
    if not stashed:
        return HttpResponseBadRequest("No OAuth flow in progress")
    flow_state = oauth.OAuthFlowState.model_validate(stashed)

    try:
        tokens = oauth.exchange_code(
            code=code,
            expected_state=flow_state.state,
            received_state=received_state,
        )
    except oauth.StateMismatchError:
        return HttpResponseBadRequest("OAuth state mismatch")

    oauth.ingest_tokens(customer=request.user, tokens=tokens)
    return redirect(_success_url())


@login_required
@require_POST
def disconnect(request: HttpRequest) -> HttpResponse:
    try:
        connection = OuraConnection.objects.get(customer=request.user)
    except OuraConnection.DoesNotExist:
        return redirect(_success_url())
    oauth.revoke(connection)
    return redirect(_success_url())


@csrf_exempt
@require_http_methods(["GET", "POST"])
def notification_receiver(request: HttpRequest) -> HttpResponse:
    """Receive Oura webhook traffic: GET verification, POST notifications."""
    if request.method == "GET":
        return _verification_challenge(request)
    return _event_notification(request)


def _verification_challenge(request: HttpRequest) -> HttpResponse:
    expected = getattr(settings, "OURA_WEBHOOK_VERIFICATION_TOKEN", "")
    token = request.GET.get("verification_token", "")
    challenge = request.GET.get("challenge", "")
    if not expected or token != expected:
        return HttpResponseForbidden("invalid verification token")
    if not challenge:
        return HttpResponseBadRequest("missing challenge")
    return JsonResponse({"challenge": challenge})


def _event_notification(request: HttpRequest) -> HttpResponse:
    """Verify the HMAC signature, then emit ``notification_received``.

    Signature enforcement is on by default; set
    ``OURA_WEBHOOK_REQUIRE_SIGNATURE = False`` to accept unsigned POSTs (a
    signature that IS present is always checked). Responding 401 (not 410 —
    that cancels the subscription) lets Oura's retries succeed once a config
    problem is fixed.
    """
    require_signature = getattr(settings, "OURA_WEBHOOK_REQUIRE_SIGNATURE", True)
    signature = request.headers.get("x-oura-signature")
    timestamp = request.headers.get("x-oura-timestamp")
    if signature or require_signature:
        if not webhooks.verify_signature(
            signature=signature, timestamp=timestamp, body=request.body
        ):
            return HttpResponse("invalid signature", status=401)

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("invalid JSON body")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("expected a JSON object")

    notification_received.send(sender=None, payload=payload)
    return HttpResponse(status=200)
