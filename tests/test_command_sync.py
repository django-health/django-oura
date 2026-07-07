from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from oura.ingest import SyncResult
from oura.models import ConnectionStatus, OuraConnection


def _run(*args) -> str:
    out = StringIO()
    call_command("sync_oura", *args, stdout=out, stderr=out)
    return out.getvalue()


def _result(**counts) -> SyncResult:
    result = SyncResult()
    result.counts = counts or {"daily_activity": 2}
    return result


@pytest.mark.django_db
class TestSyncCommand:
    def test_no_connections(self):
        assert "No matching active connections" in _run()

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_syncs_active_connections(self, sync_user, connection):
        sync_user.return_value = _result()
        output = _run()
        assert sync_user.call_count == 1
        assert "daily_activity=2" in output

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_default_window_is_one_day(self, sync_user, connection):
        sync_user.return_value = _result()
        _run()
        kwargs = sync_user.call_args.kwargs
        window = kwargs["end"] - kwargs["start"]
        assert window == timedelta(days=1)

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_days_flag(self, sync_user, connection):
        sync_user.return_value = _result()
        _run("--days", "30")
        kwargs = sync_user.call_args.kwargs
        assert kwargs["end"] - kwargs["start"] == timedelta(days=30)

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_explicit_start_end(self, sync_user, connection):
        sync_user.return_value = _result()
        _run("--start", "2026-07-01T00:00:00", "--end", "2026-07-02T00:00:00")
        kwargs = sync_user.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert kwargs["end"] == datetime(2026, 7, 2, tzinfo=timezone.utc)

    def test_start_without_end_rejected(self, connection):
        with pytest.raises(CommandError, match="together"):
            _run("--start", "2026-07-01T00:00:00")

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_data_types_forwarded(self, sync_user, connection):
        sync_user.return_value = _result()
        _run("--data-type", "sleep", "--data-type", "workout")
        assert sync_user.call_args.kwargs["data_types"] == ["sleep", "workout"]

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_user_filter(self, sync_user, connection):
        sync_user.return_value = _result()
        User = get_user_model()
        other = User.objects.create_user(username="other-user")
        OuraConnection.objects.create(
            customer=other,
            oura_user_id="other-oura-id",
            access_token="a",
            refresh_token="r",
            token_expires_at=datetime.now(timezone.utc),
        )

        _run("--user", "test-customer")

        assert sync_user.call_count == 1
        synced = sync_user.call_args.args[0]
        assert synced.customer.username == "test-customer"

    def test_unknown_user_rejected(self, connection):
        with pytest.raises(CommandError, match="No user with"):
            _run("--user", "nobody")

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_inactive_connections_skipped(self, sync_user, connection):
        connection.status = ConnectionStatus.REVOKED
        connection.save(update_fields=["status"])
        output = _run()
        assert sync_user.call_count == 0
        assert "No matching active connections" in output

    @mock.patch("oura.management.commands.sync_oura.sync_user")
    def test_one_failure_does_not_stop_the_rest(self, sync_user, connection):
        User = get_user_model()
        other = User.objects.create_user(username="other-user")
        OuraConnection.objects.create(
            customer=other,
            oura_user_id="other-oura-id",
            access_token="a",
            refresh_token="r",
            token_expires_at=datetime.now(timezone.utc),
        )
        sync_user.side_effect = [RuntimeError("boom"), _result()]

        with pytest.raises(CommandError, match="1 connection"):
            _run()

        assert sync_user.call_count == 2
