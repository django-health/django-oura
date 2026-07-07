from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from oura import oauth
from oura.constants import (
    API_BASE_URL,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
)
from oura.models import ConnectionStatus, OuraConnection

TOKEN_RESPONSE = {
    "access_token": "new-access-token",
    "token_type": "bearer",
    "expires_in": 86400,
    "refresh_token": "new-refresh-token",
    "scope": "daily heartrate workout spo2",
}

PERSONAL_INFO_URL = f"{API_BASE_URL}/v2/usercollection/personal_info"


class TestBuildAuthorizationUrl:
    def test_url_and_params(self):
        url, flow_state = oauth.build_authorization_url()
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        assert url.startswith(OAUTH_AUTHORIZATION_URL)
        assert params["response_type"] == "code"
        assert params["client_id"] == "test-client-id"
        assert params["redirect_uri"] == "http://testserver/oura/callback/"
        assert params["scope"] == "daily heartrate workout spo2"
        assert params["state"] == flow_state.state

    def test_explicit_scopes(self):
        url, _ = oauth.build_authorization_url(scopes=["daily", "email"])
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert params["scope"] == "daily email"

    def test_scopes_setting_overrides_default(self, settings):
        settings.OURA_SCOPES = ["personal"]
        url, _ = oauth.build_authorization_url()
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert params["scope"] == "personal"

    def test_explicit_state_is_used(self):
        url, flow_state = oauth.build_authorization_url(state="fixed-state")
        assert flow_state.state == "fixed-state"
        assert "state=fixed-state" in url

    def test_state_is_unique_per_call(self):
        _, first = oauth.build_authorization_url()
        _, second = oauth.build_authorization_url()
        assert first.state != second.state


class TestExchangeCode:
    @respx.mock
    def test_posts_grant_and_returns_tokens(self):
        route = respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        tokens = oauth.exchange_code(code="auth-code")

        assert tokens.access_token == "new-access-token"
        assert tokens.refresh_token == "new-refresh-token"
        assert tokens.scopes == ["daily", "heartrate", "workout", "spo2"]

        sent = dict(
            pair.split("=", 1)
            for pair in route.calls.last.request.content.decode().split("&")
        )
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "auth-code"
        assert sent["client_id"] == "test-client-id"
        assert sent["client_secret"] == "test-client-secret"

    def test_state_mismatch_raises(self):
        with pytest.raises(oauth.StateMismatchError):
            oauth.exchange_code(
                code="auth-code",
                expected_state="expected",
                received_state="tampered",
            )

    @respx.mock
    def test_non_2xx_raises_token_exchange_error(self):
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(oauth.TokenExchangeError) as excinfo:
            oauth.exchange_code(code="bad")
        assert excinfo.value.status_code == 400


@pytest.mark.django_db
class TestIngestTokens:
    @respx.mock
    def test_creates_connection_and_fetches_identity(self, customer):
        respx.get(PERSONAL_INFO_URL).mock(
            return_value=Response(200, json={"id": "oura-user-1", "age": 40})
        )
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

        connection = oauth.ingest_tokens(
            customer=customer, tokens=TOKEN_RESPONSE, now=now
        )

        assert connection.oura_user_id == "oura-user-1"
        assert connection.access_token == "new-access-token"
        assert connection.refresh_token == "new-refresh-token"
        assert connection.token_expires_at == datetime(
            2026, 7, 7, 12, 0, tzinfo=timezone.utc
        )
        assert connection.scopes == ["daily", "heartrate", "workout", "spo2"]
        assert connection.status == ConnectionStatus.ACTIVE

    @respx.mock
    def test_identity_failure_stores_empty_user_id(self, customer):
        respx.get(PERSONAL_INFO_URL).mock(return_value=Response(500, text="boom"))
        connection = oauth.ingest_tokens(customer=customer, tokens=TOKEN_RESPONSE)
        assert connection.oura_user_id == ""

    def test_explicit_id_skips_http(self, customer):
        connection = oauth.ingest_tokens(
            customer=customer,
            tokens=TOKEN_RESPONSE,
            oura_user_id="explicit-id",
        )
        assert connection.oura_user_id == "explicit-id"

    def test_reconnect_updates_existing_row(self, connection):
        updated = oauth.ingest_tokens(
            customer=connection.customer,
            tokens=TOKEN_RESPONSE,
            oura_user_id="same-user",
        )
        assert updated.pk == connection.pk
        assert OuraConnection.objects.count() == 1
        assert updated.access_token == "new-access-token"


@pytest.mark.django_db
class TestRefreshAccessToken:
    @respx.mock
    def test_rotates_both_tokens(self, connection):
        route = respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=TOKEN_RESPONSE)
        )
        oauth.refresh_access_token(connection)

        connection.refresh_from_db()
        assert connection.access_token == "new-access-token"
        # Oura refresh tokens are single-use and rotate on every refresh.
        assert connection.refresh_token == "new-refresh-token"

        sent = dict(
            pair.split("=", 1)
            for pair in route.calls.last.request.content.decode().split("&")
        )
        assert sent["grant_type"] == "refresh_token"
        assert sent["refresh_token"] == "oura-initial-refresh"


@pytest.mark.django_db
class TestRevoke:
    @respx.mock
    def test_revokes_token_and_marks_revoked(self, connection):
        route = respx.post(OAUTH_REVOKE_URL).mock(return_value=Response(200))
        oauth.revoke(connection)

        assert route.called
        assert (
            route.calls.last.request.url.params["access_token"] == "oura-initial-access"
        )
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED

    @respx.mock
    def test_oura_error_still_revokes_locally(self, connection):
        respx.post(OAUTH_REVOKE_URL).mock(return_value=Response(500))
        oauth.revoke(connection)
        connection.refresh_from_db()
        assert connection.status == ConnectionStatus.REVOKED
