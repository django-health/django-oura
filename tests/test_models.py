from datetime import datetime, timedelta, timezone

import pytest

from oura.models import ConnectionStatus, OuraConnection


@pytest.mark.django_db
class TestOuraConnection:
    def test_str(self, connection):
        assert str(connection) == (
            f"OuraConnection(customer={connection.customer_id}, status=active)"
        )

    def test_defaults(self, connection):
        assert connection.status == ConnectionStatus.ACTIVE
        assert connection.last_sync_at is None
        assert connection.connected_at is not None

    def test_one_connection_per_customer(self, connection):
        with pytest.raises(Exception):  # IntegrityError, wrapped by the backend
            OuraConnection.objects.create(
                customer=connection.customer,
                oura_user_id="other",
                access_token="a",
                refresh_token="r",
                token_expires_at=datetime.now(timezone.utc),
            )


class TestIsTokenExpired:
    def _connection(self, expires_at: datetime) -> OuraConnection:
        return OuraConnection(token_expires_at=expires_at)

    def test_fresh_token(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        connection = self._connection(now + timedelta(hours=12))
        assert connection.is_token_expired(now=now) is False

    def test_expired_token(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        connection = self._connection(now - timedelta(minutes=1))
        assert connection.is_token_expired(now=now) is True

    def test_leeway_counts_as_expired(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        connection = self._connection(now + timedelta(seconds=30))
        # 30s from expiry is inside the default 60s leeway.
        assert connection.is_token_expired(now=now) is True
        assert connection.is_token_expired(leeway_seconds=0, now=now) is False
