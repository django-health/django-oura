import pytest
import respx
from healthdatamodel.models import Record, Workout
from httpx import Response

from oura import webhooks
from oura.constants import API_BASE_URL

COLLECTION = f"{API_BASE_URL}/v2/usercollection"
SUBSCRIPTION = f"{API_BASE_URL}/v2/webhook/subscription"

SLEEP_DOCUMENT = {
    "id": "sl-1",
    "day": "2026-07-05",
    "type": "long_sleep",
    "bedtime_start": "2026-07-04T23:00:00-04:00",
    "bedtime_end": "2026-07-05T07:00:00-04:00",
    "sleep_phase_5_min": "44112233",
}

WORKOUT_DOCUMENT = {
    "id": "wo-1",
    "activity": "cycling",
    "start_datetime": "2026-07-05T10:00:00-04:00",
    "end_datetime": "2026-07-05T11:00:00-04:00",
    "calories": 600,
}


def _notification(**overrides):
    payload = {
        "event_type": "update",
        "data_type": "sleep",
        "object_id": "sl-1",
        "event_time": "2026-07-05T08:00:00+00:00",
        "user_id": "e4b8f9d2-3c6a-4f7e-9d1b-2a5c8e7f6a3b",
    }
    payload.update(overrides)
    return payload


class TestSignature:
    def test_round_trip(self):
        body = b'{"event_type": "update"}'
        signature = webhooks.compute_signature(
            timestamp="123", body=body, client_secret="s3cret"
        )
        assert signature == signature.upper()
        assert webhooks.verify_signature(
            signature=signature, timestamp="123", body=body, client_secret="s3cret"
        )

    def test_tampered_body_fails(self):
        signature = webhooks.compute_signature(
            timestamp="123", body=b"a", client_secret="s3cret"
        )
        assert not webhooks.verify_signature(
            signature=signature, timestamp="123", body=b"b", client_secret="s3cret"
        )

    def test_missing_parts_fail(self):
        assert not webhooks.verify_signature(
            signature=None, timestamp="123", body=b"a", client_secret="s"
        )
        assert not webhooks.verify_signature(
            signature="X", timestamp=None, body=b"a", client_secret="s"
        )


@pytest.mark.django_db
class TestProcessNotification:
    @respx.mock
    def test_fetches_document_and_ingests(self, connection):
        route = respx.get(f"{COLLECTION}/sleep/sl-1").mock(
            return_value=Response(200, json=SLEEP_DOCUMENT)
        )
        count = webhooks.process_notification(_notification())

        assert route.called
        assert count == 4  # awake, deep, light, rem runs
        assert Record.objects.filter(customer=connection.customer).count() == 4
        connection.refresh_from_db()
        assert connection.last_sync_at is not None

    @respx.mock
    def test_workout_notification_creates_workout(self, connection):
        respx.get(f"{COLLECTION}/workout/wo-1").mock(
            return_value=Response(200, json=WORKOUT_DOCUMENT)
        )
        count = webhooks.process_notification(
            _notification(data_type="workout", object_id="wo-1")
        )
        assert count == 1
        assert Workout.objects.count() == 1

    def test_unknown_user_skipped(self, connection):
        count = webhooks.process_notification(_notification(user_id="stranger"))
        assert count == 0
        assert Record.objects.count() == 0

    def test_delete_event_skipped(self, connection):
        count = webhooks.process_notification(_notification(event_type="delete"))
        assert count == 0

    def test_unmapped_data_type_skipped(self, connection):
        count = webhooks.process_notification(_notification(data_type="daily_stress"))
        assert count == 0

    def test_missing_ids_skipped(self, connection):
        assert webhooks.process_notification(_notification(object_id="")) == 0
        assert webhooks.process_notification(_notification(user_id="")) == 0


class TestSubscriptionManagement:
    @respx.mock
    def test_create_sends_app_credentials(self):
        route = respx.post(SUBSCRIPTION).mock(
            return_value=Response(
                201,
                json={
                    "id": "sub-1",
                    "callback_url": "https://example.com/oura/notifications/",
                    "event_type": "update",
                    "data_type": "sleep",
                    "expiration_time": "2026-10-04T00:00:00+00:00",
                },
            )
        )
        subscription = webhooks.create_subscription(
            callback_url="https://example.com/oura/notifications/",
            verification_token="tok",
            event_type="update",
            data_type="sleep",
        )
        assert subscription["id"] == "sub-1"
        request = route.calls.last.request
        assert request.headers["x-client-id"] == "test-client-id"
        assert request.headers["x-client-secret"] == "test-client-secret"

    @respx.mock
    def test_list(self):
        respx.get(SUBSCRIPTION).mock(
            return_value=Response(200, json=[{"id": "sub-1"}, {"id": "sub-2"}])
        )
        assert [s["id"] for s in webhooks.list_subscriptions()] == ["sub-1", "sub-2"]

    @respx.mock
    def test_renew(self):
        route = respx.put(f"{SUBSCRIPTION}/renew/sub-1").mock(
            return_value=Response(200, json={"id": "sub-1"})
        )
        webhooks.renew_subscription("sub-1")
        assert route.called

    @respx.mock
    def test_delete(self):
        route = respx.delete(f"{SUBSCRIPTION}/sub-1").mock(return_value=Response(204))
        webhooks.delete_subscription("sub-1")
        assert route.called

    @respx.mock
    def test_error_raises_webhook_error(self):
        respx.get(SUBSCRIPTION).mock(return_value=Response(403, text="nope"))
        with pytest.raises(webhooks.WebhookError) as excinfo:
            webhooks.list_subscriptions()
        assert excinfo.value.status_code == 403
