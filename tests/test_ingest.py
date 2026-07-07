"""Mapper + orchestrator tests.

Fixture payloads follow the shapes in the Oura v2 OpenAPI spec (1.35) —
see ``docs/oura/api.md``. Localized timestamps deliberately use a non-UTC
offset to prove the UTC conversion.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from healthdatamodel.models import Record, Workout
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, SleepValue

from oura import ingest
from oura.ingest import (
    DEFAULT_DATA_TYPES,
    SyncResult,
    ingest_documents,
    map_daily_activity,
    map_daily_spo2,
    map_heartrate,
    map_sleep,
    map_workout,
    sync_user,
)

DAILY_ACTIVITY = {
    "id": "da-1",
    "day": "2026-07-04",
    "timestamp": "2026-07-04T04:00:00-04:00",
    "steps": 8210,
    "active_calories": 512,
    "total_calories": 2698,
    "equivalent_walking_distance": 6300,
    "score": 85,
}

SLEEP = {
    "id": "sl-1",
    "day": "2026-07-05",
    "type": "long_sleep",
    "bedtime_start": "2026-07-04T23:00:00-04:00",
    "bedtime_end": "2026-07-04T23:22:00-04:00",
    # 4 chars ≈ 20 min: awake, deep, deep, light. Session ends at 22 min, so
    # the light run (15–20 min nominal) stays within bedtime_end.
    "sleep_phase_5_min": "4112",
    "total_sleep_duration": 1200,
}

DAILY_SPO2 = {
    "id": "sp-1",
    "day": "2026-07-04",
    "spo2_percentage": {"average": 97.5},
    "breathing_disturbance_index": 2,
}

HEARTRATE_ROWS = [
    {
        "bpm": 61,
        "source": "sleep",
        "timestamp": "2026-07-04T23:05:00-04:00",
        "timestamp_unix": 1751684700000,
    },
    {
        "bpm": 88,
        "source": "awake",
        "timestamp": "2026-07-05T09:05:00-04:00",
        "timestamp_unix": 1751720700000,
    },
]

WORKOUT = {
    "id": "wo-1",
    "day": "2026-07-04",
    "activity": "running",
    "start_datetime": "2026-07-04T08:00:00-04:00",
    "end_datetime": "2026-07-04T08:45:00-04:00",
    "calories": 410.5,
    "distance": 7500.0,
    "intensity": "hard",
    "label": "morning tempo",
    "source": "confirmed",
}


class TestMapDailyActivity:
    def test_metrics_and_utc_day_bounds(self):
        records = map_daily_activity(DAILY_ACTIVITY)
        by_type = {r.type: r for r in records}

        steps = by_type[str(ActivityMetric.STEPS)]
        assert steps.value == "8210"
        # 04:00-04:00 == 08:00 UTC.
        assert steps.startDate == datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc)
        assert steps.endDate == steps.startDate + timedelta(hours=24)
        assert steps.recordId == "da-1-steps"
        assert steps.sourceName == "Oura"

        assert by_type[str(ActivityMetric.ACTIVE_CALORIES)].value == "512"
        # Basal = total − active.
        assert by_type[str(ActivityMetric.BASAL_CALORIES)].value == "2186"
        assert by_type[ingest.HK_DISTANCE_WALKING_RUNNING].value == "6300"

    def test_missing_metrics_are_skipped(self):
        records = map_daily_activity(
            {"id": "da-2", "timestamp": "2026-07-04T04:00:00-04:00", "steps": 100}
        )
        assert [r.type for r in records] == [str(ActivityMetric.STEPS)]


class TestMapSleep:
    def test_phase_runs_become_stage_records(self):
        records = map_sleep(SLEEP)
        start = datetime(2026, 7, 5, 3, 0, tzinfo=timezone.utc)  # 23:00-04:00

        assert [r.value for r in records] == [
            SleepValue.AWAKE,
            SleepValue.ASLEEP_DEEP,
            SleepValue.ASLEEP_CORE,
        ]
        assert all(r.type == SLEEP_TYPE for r in records)

        awake, deep, light = records
        assert awake.startDate == start
        assert awake.endDate == start + timedelta(minutes=5)
        # The two '1' chars collapse into one 10-minute deep run.
        assert deep.startDate == start + timedelta(minutes=5)
        assert deep.endDate == start + timedelta(minutes=15)
        assert deep.recordId == "sl-1-phase-1"

    def test_final_run_clamped_to_bedtime_end(self):
        records = map_sleep(SLEEP)
        bedtime_end = datetime(2026, 7, 5, 3, 22, tzinfo=timezone.utc)
        # Nominal end of the last 5-min slot is 3:20 (< bedtime_end): fine.
        assert records[-1].endDate <= bedtime_end

        long_phases = dict(SLEEP, sleep_phase_5_min="41122")
        clamped = map_sleep(long_phases)
        # The last run's nominal end (25 min) exceeds the 22-minute session.
        assert clamped[-1].endDate == bedtime_end

    def test_no_phases_falls_back_to_duration_anchored_session(self):
        document = dict(SLEEP, sleep_phase_5_min=None)
        records = map_sleep(document)
        assert len(records) == 1
        assert records[0].value == SleepValue.ASLEEP_UNSPECIFIED
        # total_sleep_duration (1200s) anchored at bedtime_end — not the whole
        # bedtime span, which includes in-bed-awake time.
        assert records[0].endDate == datetime(2026, 7, 5, 3, 22, tzinfo=timezone.utc)
        assert records[0].startDate == datetime(2026, 7, 5, 3, 2, tzinfo=timezone.utc)

    def test_fallback_without_duration_uses_bedtime_span(self):
        document = dict(SLEEP, sleep_phase_5_min=None, total_sleep_duration=None)
        records = map_sleep(document)
        assert records[0].startDate == datetime(2026, 7, 5, 3, 0, tzinfo=timezone.utc)
        assert records[0].endDate == datetime(2026, 7, 5, 3, 22, tzinfo=timezone.utc)

    def test_degenerate_bedtime_span_still_yields_duration(self):
        # Oura's sandbox (and some nap documents) report bedtime_start ==
        # bedtime_end with a real total_sleep_duration.
        document = {
            "id": "sl-nap",
            "type": "late_nap",
            "bedtime_start": "2026-06-30T00:00:00.000+00:00",
            "bedtime_end": "2026-06-30T00:00:00.000+00:00",
            "sleep_phase_5_min": None,
            "total_sleep_duration": 2370,
        }
        records = map_sleep(document)
        assert len(records) == 1
        assert records[0].endDate == datetime(2026, 6, 30, tzinfo=timezone.utc)
        assert records[0].endDate - records[0].startDate == timedelta(seconds=2370)

    def test_deleted_tombstone_yields_nothing(self):
        assert map_sleep(dict(SLEEP, type="deleted")) == []


class TestMapDailySpo2:
    def test_average_spans_day(self):
        records = map_daily_spo2(DAILY_SPO2)
        assert len(records) == 1
        record = records[0]
        assert record.type == ingest.HK_OXYGEN_SATURATION
        assert record.value == "97.5"
        assert record.unit == "%"
        assert record.startDate == datetime(2026, 7, 4, tzinfo=timezone.utc)
        assert record.endDate == datetime(2026, 7, 5, tzinfo=timezone.utc)

    def test_missing_aggregate_yields_nothing(self):
        assert map_daily_spo2({"id": "sp-2", "day": "2026-07-04"}) == []


class TestMapHeartrate:
    def test_one_record_per_sample(self):
        records = map_heartrate(HEARTRATE_ROWS)
        assert len(records) == 2
        first = records[0]
        assert first.type == ingest.HK_HEART_RATE
        assert first.value == "61"
        assert first.unit == "count/min"
        # 23:05-04:00 == 03:05 UTC; instant records have start == end.
        assert first.startDate == datetime(2026, 7, 5, 3, 5, tzinfo=timezone.utc)
        assert first.startDate == first.endDate

    def test_rows_without_bpm_are_skipped(self):
        assert map_heartrate([{"timestamp": "2026-07-05T00:00:00+00:00"}]) == []


class TestMapWorkout:
    def test_fields_and_units(self):
        workout = map_workout(WORKOUT)
        assert workout.recordId == "wo-1"
        assert workout.workoutActivityType == "running"
        assert workout.startDate == datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        assert workout.duration == 45 * 60
        assert workout.durationUnit == "s"
        assert workout.caloriesBurned == 410.5
        assert workout.caloriesUnit == "kcal"
        assert workout.distance == 7.5
        assert workout.distanceUnit == "km"
        metadata = {entry.key: entry.value for entry in workout.metadataEntry}
        assert metadata == {
            "label": "morning tempo",
            "intensity": "hard",
            "source": "confirmed",
        }

    def test_optional_fields_absent(self):
        workout = map_workout(
            {
                "id": "wo-2",
                "activity": "walking",
                "start_datetime": "2026-07-04T08:00:00+00:00",
                "end_datetime": "2026-07-04T08:30:00+00:00",
            }
        )
        assert workout.caloriesBurned is None
        assert workout.distance is None
        assert workout.metadataEntry is None


@pytest.mark.django_db
class TestIngestDocuments:
    def test_records_persisted_with_oura_source(self, connection):
        count = ingest_documents(connection, "daily_activity", [DAILY_ACTIVITY])
        assert count == 4
        assert Record.objects.count() == 4
        record = Record.objects.first()
        assert record.source == "oura"
        assert record.sourceName == "Oura"
        assert record.customer == connection.customer

    def test_workouts_persisted(self, connection):
        count = ingest_documents(connection, "workout", [WORKOUT])
        assert count == 1
        workout = Workout.objects.get()
        assert workout.source == "oura"
        assert workout.workoutActivityType == "running"

    def test_heartrate_rows_persisted(self, connection):
        count = ingest_documents(connection, "heartrate", HEARTRATE_ROWS)
        assert count == 2
        assert Record.objects.count() == 2

    def test_unknown_data_type_skipped(self, connection):
        assert ingest_documents(connection, "daily_stress", [{"id": "x"}]) == 0
        assert Record.objects.count() == 0


class FakeClient:
    """Stands in for OuraClient in orchestrator tests."""

    def __init__(self, documents: dict[str, list[dict[str, Any]]]):
        self.documents = documents
        self.calls: list[tuple] = []
        self.closed = False

    def list_documents(self, data_type, *, start_date, end_date):
        self.calls.append((data_type, start_date, end_date))
        return self.documents.get(data_type, [])

    def list_heartrate(self, *, start, end):
        self.calls.append(("heartrate", start, end))
        return self.documents.get("heartrate", [])

    def close(self):
        self.closed = True


@pytest.mark.django_db
class TestSyncUser:
    def test_syncs_all_default_types(self, connection):
        client = FakeClient(
            {
                "daily_activity": [DAILY_ACTIVITY],
                "sleep": [SLEEP],
                "daily_spo2": [DAILY_SPO2],
                "heartrate": HEARTRATE_ROWS,
                "workout": [WORKOUT],
            }
        )
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 6, tzinfo=timezone.utc)

        result = sync_user(connection, start=start, end=end, client=client)

        assert isinstance(result, SyncResult)
        assert set(result.counts) == set(DEFAULT_DATA_TYPES)
        assert result.counts == {
            "daily_activity": 4,
            "sleep": 3,
            "daily_spo2": 1,
            "heartrate": 2,
            "workout": 1,
        }
        assert result.total == 11

        # Document routes get dates; the heartrate route gets instants.
        assert ("daily_activity", date(2026, 7, 1), date(2026, 7, 6)) in client.calls
        assert ("heartrate", start, end) in client.calls

        connection.refresh_from_db()
        assert connection.last_sync_at is not None
        # A caller-supplied client is not closed by sync_user.
        assert client.closed is False

    def test_restricts_to_requested_types(self, connection):
        client = FakeClient({"daily_activity": [DAILY_ACTIVITY]})
        result = sync_user(
            connection,
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 2, tzinfo=timezone.utc),
            data_types=["daily_activity"],
            client=client,
        )
        assert list(result.counts) == ["daily_activity"]
        assert len(client.calls) == 1
