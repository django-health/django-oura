import json
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from django.test import Client
from httpx import Response

from oura.constants import API_BASE_URL, OAUTH_TOKEN_URL
from oura.models import ConnectionStatus, OuraConnection
from oura.signals import notification_received
from oura.views import SESSION_KEY
from oura.webhooks import compute_signature

TOKEN_RESPONSE = {
    "access_token": "cb-access-token",
    "token_type": "bearer",
    "expires_in": 86400,
    "refresh_token": "cb-refresh-token",
    "scope": "daily",
}


@pytest.fixture
def http(customer):
    client = Client()
    client.force_login(customer)
    return client


@pytest.mark.django_db
class TestConnect:
    def test_redirects_to_oura_and_stashes_state(self, http):
        response = http.get("/oura/connect/")
        assert response.status_code == 302
        assert response.url.startswith("https://cloud.ouraring.com/oauth/authorize?")

        params = {k: v[0] for k, v in parse_qs(urlparse(response.url).query).items()}
        stashed = http.session[SESSION_KEY]
        assert params["state"] == stashed["state"]

    def test_requires_login(self):
        response = Client().get("/oura/connect/")
        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestCallback:
    def _start_flow(self, http) -> str:
        http.get("/oura/connect/")
        return http.session[SESSION_KEY]["state"]

    @respx.mock
    def test_happy_path_creates_connection(self, http, customer):
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        respx.get(f"{API_BASE_URL}/v2/usercollection/personal_info").mock(
            return_value=Response(200, json={"id": "oura-user-9"})
        )
        state = self._start_flow(http)

        response = http.get("/oura/callback/", {"code": "auth-code", "state": state})

        assert response.status_code == 302
        connection = OuraConnection.objects.get(customer=customer)
        assert connection.access_token == "cb-access-token"
        assert connection.oura_user_id == "oura-user-9"
        assert connection.status == ConnectionStatus.ACTIVE

    def test_state_mismatch_rejected(self, http):
        self._start_flow(http)
        response = http.get(
            "/oura/callback/", {"code": "auth-code", "state": "tampered"}
        )
        assert response.status_code == 400
        assert OuraConnection.objects.count() == 0

    def test_error_param_rejected(self, http):
        response = http.get("/oura/callback/", {"error": "access_denied"})
        assert response.status_code == 400

    def test_no_flow_in_progress_rejected(self, http):
        response = http.get("/oura/callback/", {"code": "auth-code", "state": "x"})
        assert response.status_code == 400


@pytest.mark.django_db
class TestDisconnect:
    @respx.mock
    def test_revokes_connection(self, http, connection):
        respx.post("https://api.ouraring.com/oauth/revoke").mock(
            return_value=Response(200)
        )
        response = http.post("/oura/disconnect/")
        assert response.status_code == 302
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED

    def test_without_connection_redirects(self, http):
        response = http.post("/oura/disconnect/")
        assert response.status_code == 302


def _signed_headers(body: bytes, timestamp: str = "1751770000") -> dict:
    return {
        "HTTP_X_OURA_SIGNATURE": compute_signature(
            timestamp=timestamp, body=body, client_secret="test-client-secret"
        ),
        "HTTP_X_OURA_TIMESTAMP": timestamp,
    }


@pytest.mark.django_db
class TestNotificationReceiverVerification:
    def test_challenge_echoed_for_valid_token(self, client):
        response = client.get(
            "/oura/notifications/",
            {"verification_token": "test-verification-token", "challenge": "rand-42"},
        )
        assert response.status_code == 200
        assert response.json() == {"challenge": "rand-42"}

    def test_wrong_token_forbidden(self, client):
        response = client.get(
            "/oura/notifications/",
            {"verification_token": "wrong", "challenge": "rand-42"},
        )
        assert response.status_code == 403

    def test_unconfigured_token_forbidden(self, client, settings):
        settings.OURA_WEBHOOK_VERIFICATION_TOKEN = ""
        response = client.get(
            "/oura/notifications/",
            {"verification_token": "", "challenge": "rand-42"},
        )
        assert response.status_code == 403

    def test_missing_challenge_rejected(self, client):
        response = client.get(
            "/oura/notifications/",
            {"verification_token": "test-verification-token"},
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestNotificationReceiverEvents:
    PAYLOAD = {
        "event_type": "update",
        "data_type": "sleep",
        "object_id": "sl-1",
        "event_time": "2026-07-05T08:00:00+00:00",
        "user_id": "oura-user-1",
    }

    def test_valid_signature_emits_signal(self, client):
        received = []

        def handler(sender, payload, **kwargs):
            received.append(payload)

        notification_received.connect(handler)
        try:
            body = json.dumps(self.PAYLOAD).encode()
            response = client.post(
                "/oura/notifications/",
                data=body,
                content_type="application/json",
                **_signed_headers(body),
            )
        finally:
            notification_received.disconnect(handler)

        assert response.status_code == 200
        assert received == [self.PAYLOAD]

    def test_bad_signature_rejected(self, client):
        body = json.dumps(self.PAYLOAD).encode()
        response = client.post(
            "/oura/notifications/",
            data=body,
            content_type="application/json",
            HTTP_X_OURA_SIGNATURE="0" * 64,
            HTTP_X_OURA_TIMESTAMP="1751770000",
        )
        assert response.status_code == 401

    def test_missing_signature_rejected_by_default(self, client):
        response = client.post(
            "/oura/notifications/",
            data=json.dumps(self.PAYLOAD),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_missing_signature_allowed_when_not_required(self, client, settings):
        settings.OURA_WEBHOOK_REQUIRE_SIGNATURE = False
        received = []

        def handler(sender, payload, **kwargs):
            received.append(payload)

        notification_received.connect(handler)
        try:
            response = client.post(
                "/oura/notifications/",
                data=json.dumps(self.PAYLOAD),
                content_type="application/json",
            )
        finally:
            notification_received.disconnect(handler)

        assert response.status_code == 200
        assert len(received) == 1

    def test_present_signature_still_checked_when_not_required(self, client, settings):
        settings.OURA_WEBHOOK_REQUIRE_SIGNATURE = False
        response = client.post(
            "/oura/notifications/",
            data=json.dumps(self.PAYLOAD),
            content_type="application/json",
            HTTP_X_OURA_SIGNATURE="0" * 64,
            HTTP_X_OURA_TIMESTAMP="1751770000",
        )
        assert response.status_code == 401

    def test_invalid_json_rejected(self, client):
        body = b"not json"
        response = client.post(
            "/oura/notifications/",
            data=body,
            content_type="application/json",
            **_signed_headers(body),
        )
        assert response.status_code == 400
