from datetime import date, datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from oura.client import OuraAPIError, OuraClient
from oura.constants import API_BASE_URL, OAUTH_TOKEN_URL

COLLECTION = f"{API_BASE_URL}/v2/usercollection"

START = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(days=7)

REFRESH_RESPONSE = {
    "access_token": "refreshed-access",
    "expires_in": 86400,
    "refresh_token": "refreshed-refresh",
}


@pytest.mark.django_db
class TestRequestLoop:
    @respx.mock
    def test_sends_bearer_header(self, connection):
        route = respx.get(f"{COLLECTION}/personal_info").mock(
            return_value=Response(200, json={"id": "u1"})
        )
        with OuraClient(connection) as client:
            assert client.get_personal_info() == {"id": "u1"}
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == "Bearer oura-initial-access"

    @respx.mock
    def test_proactive_refresh_when_expired(self, connection):
        connection.token_expires_at = datetime.now(timezone.utc)
        connection.save(update_fields=["token_expires_at"])
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=REFRESH_RESPONSE)
        )
        route = respx.get(f"{COLLECTION}/personal_info").mock(
            return_value=Response(200, json={"id": "u1"})
        )
        with OuraClient(connection) as client:
            client.get_personal_info()
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == "Bearer refreshed-access"

    @respx.mock
    def test_retries_once_after_401(self, connection):
        respx.post(OAUTH_TOKEN_URL).mock(
            return_value=Response(200, json=REFRESH_RESPONSE)
        )
        route = respx.get(f"{COLLECTION}/personal_info").mock(
            side_effect=[
                Response(401),
                Response(200, json={"id": "u1"}),
            ]
        )
        with OuraClient(connection) as client:
            assert client.get_personal_info() == {"id": "u1"}
        assert route.call_count == 2

    @respx.mock
    def test_retries_on_429_honoring_retry_after(self, connection):
        sleeps: list[float] = []
        route = respx.get(f"{COLLECTION}/personal_info").mock(
            side_effect=[
                Response(429, headers={"Retry-After": "7"}),
                Response(200, json={"id": "u1"}),
            ]
        )
        with OuraClient(connection, sleep=sleeps.append) as client:
            assert client.get_personal_info() == {"id": "u1"}
        assert route.call_count == 2
        assert sleeps == [7.0]

    @respx.mock
    def test_gives_up_after_max_retries(self, connection):
        respx.get(f"{COLLECTION}/personal_info").mock(return_value=Response(503))
        with OuraClient(connection, max_retries=2, sleep=lambda _s: None) as client:
            with pytest.raises(OuraAPIError) as excinfo:
                client.get_personal_info()
        assert excinfo.value.status_code == 503

    @respx.mock
    def test_error_payload_message_surfaced(self, connection):
        respx.get(f"{COLLECTION}/personal_info").mock(
            return_value=Response(
                400, json={"detail": "Query Parameter Validation Error"}
            )
        )
        with OuraClient(connection) as client:
            with pytest.raises(OuraAPIError, match="Query Parameter"):
                client.get_personal_info()


@pytest.mark.django_db
class TestDocuments:
    @respx.mock
    def test_list_documents_sends_date_window(self, connection):
        route = respx.get(f"{COLLECTION}/daily_activity").mock(
            return_value=Response(
                200, json={"data": [{"id": "d1"}], "next_token": None}
            )
        )
        with OuraClient(connection) as client:
            documents = client.list_documents(
                "daily_activity", start_date=date(2026, 7, 1), end_date=date(2026, 7, 7)
            )
        assert documents == [{"id": "d1"}]
        params = route.calls.last.request.url.params
        assert params["start_date"] == "2026-07-01"
        assert params["end_date"] == "2026-07-07"

    @respx.mock
    def test_list_documents_follows_next_token(self, connection):
        route = respx.get(f"{COLLECTION}/sleep").mock(
            side_effect=[
                Response(200, json={"data": [{"id": "s1"}], "next_token": "page2"}),
                Response(200, json={"data": [{"id": "s2"}], "next_token": None}),
            ]
        )
        with OuraClient(connection) as client:
            documents = client.list_documents(
                "sleep", start_date=date(2026, 7, 1), end_date=date(2026, 7, 7)
            )
        assert [d["id"] for d in documents] == ["s1", "s2"]
        assert route.call_count == 2
        second_params = route.calls.last.request.url.params
        assert second_params["next_token"] == "page2"
        # Original window params are preserved alongside the token.
        assert second_params["start_date"] == "2026-07-01"

    @respx.mock
    def test_get_document(self, connection):
        route = respx.get(f"{COLLECTION}/sleep/abc123").mock(
            return_value=Response(200, json={"id": "abc123"})
        )
        with OuraClient(connection) as client:
            document = client.get_document("sleep", "abc123")
        assert document == {"id": "abc123"}
        assert route.called

    @respx.mock
    def test_list_heartrate_sends_datetime_window(self, connection):
        route = respx.get(f"{COLLECTION}/heartrate").mock(
            return_value=Response(
                200,
                json={
                    "data": [{"bpm": 62, "timestamp": "2026-07-01T00:05:00+00:00"}],
                    "next_token": None,
                },
            )
        )
        with OuraClient(connection) as client:
            rows = client.list_heartrate(start=START, end=END)
        assert rows[0]["bpm"] == 62
        params = route.calls.last.request.url.params
        assert params["start_datetime"] == START.isoformat()
        assert params["end_datetime"] == END.isoformat()
