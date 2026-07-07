"""Map Oura API v2 documents onto django-healthdatamodel records.

Mappers cover the beachhead data types:

  daily_activity → list[:class:`RecordInput`] (steps, active/basal calories,
                   equivalent walking distance)
  sleep          → list[:class:`RecordInput`] (one per sleep-phase interval)
  daily_spo2     → list[:class:`RecordInput`] (daily SpO2 average)
  heartrate      → list[:class:`RecordInput`] (one per 5-minute sample)
  workout        → :class:`WorkoutInput`

Oura reports total and active calories per day; basal is derived as their
difference (``total_calories - active_calories``) — the same decomposition the
legacy wellrider Oura V1 integration used. There is no BMR-estimation step
like django-google-health has.

Timestamps: Oura's ``LocalizedDateTime`` values carry the user's UTC offset
(e.g. ``2026-07-04T23:12:00-04:00``). Records store absolute UTC instants, so
every parsed datetime is converted with ``astimezone(utc)`` — never parsed
through ``RecordInput``'s string coercion, which assumes naive-UTC strings.

Sleep phases come from ``sleep_phase_5_min``, a string where each character
classifies five minutes from ``bedtime_start``: '1' deep, '2' light, '3' REM,
'4' awake. Runs of the same digit collapse into one record; the final interval
is clamped to ``bedtime_end``. Documents without a phase string (e.g. naps
from older rings) fall back to one ASLEEP_UNSPECIFIED record spanning the
session.

The high-level orchestrator is :func:`sync_user`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

import dateutil.parser
from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records, ingest_workouts
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, SleepValue
from healthdatamodel.schemas import MetadataEntry, RecordInput, WorkoutInput

from .client import OuraClient
from .constants import (
    DATA_TYPE_DAILY_ACTIVITY,
    DATA_TYPE_DAILY_SPO2,
    DATA_TYPE_HEARTRATE,
    DATA_TYPE_SLEEP,
    DATA_TYPE_WORKOUT,
    SOURCE_NAME,
)

if TYPE_CHECKING:
    from .models import OuraConnection

# Apple HealthKit identifiers not exported as enum members in healthdatamodel.
HK_HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
HK_DISTANCE_WALKING_RUNNING = "HKQuantityTypeIdentifierDistanceWalkingRunning"
HK_OXYGEN_SATURATION = "HKQuantityTypeIdentifierOxygenSaturation"

# sleep_phase_5_min characters → healthdatamodel SleepValue.
_SLEEP_PHASE_MAP: dict[str, str] = {
    "1": SleepValue.ASLEEP_DEEP,
    "2": SleepValue.ASLEEP_CORE,
    "3": SleepValue.ASLEEP_REM,
    "4": SleepValue.AWAKE,
}
_SLEEP_PHASE_SECONDS = 5 * 60

# Sleep documents whose ``type`` is "deleted" are tombstones for periods the
# user removed in the app; they carry no usable data.
_SLEEP_TYPE_DELETED = "deleted"


@dataclass
class SyncResult:
    """Per-type counts returned by :func:`sync_user`."""

    counts: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _utc(value: str) -> datetime:
    """Parse an Oura LocalizedDateTime/UtcDateTime string to an aware UTC instant."""
    return dateutil.parser.isoparse(value).astimezone(timezone.utc)


def _record(
    document: dict[str, Any],
    *,
    suffix: str,
    start: datetime,
    end: datetime,
    type: str,
    value: Any,
    unit: str | None,
) -> RecordInput:
    """Build a RecordInput keyed off the document's ``id``.

    One Oura document fans out into several Records (steps, calories, …), so
    the per-metric ``suffix`` keeps recordIds unique. Oura re-sends a document
    with the same id when it's updated (webhook ``update`` events), and the
    stable recordId lets the ingest layer upsert once it supports that.
    """
    document_id = document.get("id")
    return RecordInput(
        recordId=f"{document_id}-{suffix}" if document_id else None,
        startDate=start,
        endDate=end,
        creationDate=start,
        sourceName=SOURCE_NAME,
        type=type,
        value=str(value),
        unit=unit,
    )


# Mappers ---------------------------------------------------------------------


def map_daily_activity(document: dict[str, Any]) -> list[RecordInput]:
    """Fan a daily activity document out into one Record per available metric.

    ``timestamp`` is the start of the user's activity day (04:00 local); the
    day spans 24 hours from there.
    """
    start = _utc(document["timestamp"])
    end = start + timedelta(hours=24)
    records: list[RecordInput] = []

    def add(suffix: str, type: str, unit: str, value: Any) -> None:
        if value is None:
            return
        records.append(
            _record(
                document,
                suffix=suffix,
                start=start,
                end=end,
                type=type,
                value=value,
                unit=unit,
            )
        )

    add("steps", str(ActivityMetric.STEPS), "count", document.get("steps"))
    add(
        "active-kcal",
        str(ActivityMetric.ACTIVE_CALORIES),
        "kcal",
        document.get("active_calories"),
    )

    # Oura reports the day's total and active expenditure; basal is their
    # difference.
    total = document.get("total_calories")
    active = document.get("active_calories")
    if total is not None and active is not None:
        add("basal-kcal", str(ActivityMetric.BASAL_CALORIES), "kcal", total - active)

    add(
        "distance",
        HK_DISTANCE_WALKING_RUNNING,
        "m",
        document.get("equivalent_walking_distance"),
    )

    return records


def map_sleep(document: dict[str, Any]) -> list[RecordInput]:
    """Decompose a sleep document into one Record per sleep-phase run.

    Documents with ``type == "deleted"`` are tombstones and yield nothing.
    Documents without ``sleep_phase_5_min`` fall back to a single
    ASLEEP_UNSPECIFIED record over the whole session.
    """
    if document.get("type") == _SLEEP_TYPE_DELETED:
        return []

    start = _utc(document["bedtime_start"])
    end = _utc(document["bedtime_end"])
    records: list[RecordInput] = []

    phases = document.get("sleep_phase_5_min") or ""
    run_index = 0
    position = 0
    while position < len(phases):
        digit = phases[position]
        run_end = position
        while run_end < len(phases) and phases[run_end] == digit:
            run_end += 1
        mapped = _SLEEP_PHASE_MAP.get(digit)
        if mapped is not None:
            interval_start = start + timedelta(seconds=position * _SLEEP_PHASE_SECONDS)
            interval_end = min(
                start + timedelta(seconds=run_end * _SLEEP_PHASE_SECONDS), end
            )
            if interval_end > interval_start:
                records.append(
                    _record(
                        document,
                        suffix=f"phase-{run_index}",
                        start=interval_start,
                        end=interval_end,
                        type=SLEEP_TYPE,
                        value=mapped,
                        unit=None,
                    )
                )
                run_index += 1
        position = run_end

    if not records:
        # Anchor the fallback at bedtime_end minus the reported sleep total
        # (the legacy wellrider decomposition): spanning the whole bedtime
        # would overstate sleep by the in-bed-awake time, and some documents
        # (naps, sandbox data) carry a degenerate zero-length bedtime span.
        total = document.get("total_sleep_duration")
        fallback_start = end - timedelta(seconds=total) if total else start
        records.append(
            _record(
                document,
                suffix="session",
                start=fallback_start,
                end=end,
                type=SLEEP_TYPE,
                value=SleepValue.ASLEEP_UNSPECIFIED,
                unit=None,
            )
        )
    return records


def map_daily_spo2(document: dict[str, Any]) -> list[RecordInput]:
    """The daily SpO2 average during sleep, spanning the document's day.

    The document carries only a calendar ``day`` (no timestamp), so the day is
    anchored at UTC midnight.
    """
    aggregates = document.get("spo2_percentage") or {}
    average = aggregates.get("average")
    if average is None:
        return []
    start = datetime.fromisoformat(document["day"]).replace(tzinfo=timezone.utc)
    return [
        _record(
            document,
            suffix="spo2-avg",
            start=start,
            end=start + timedelta(hours=24),
            type=HK_OXYGEN_SATURATION,
            value=average,
            unit="%",
        )
    ]


def map_heartrate(rows: list[dict[str, Any]]) -> list[RecordInput]:
    """One Record per heart-rate sample (5-minute increments).

    The heart-rate route returns bare rows, not documents — there is no ``id``
    to derive a recordId from, so it stays None.
    """
    records: list[RecordInput] = []
    for row in rows:
        if row.get("bpm") is None:
            continue
        instant = _utc(row["timestamp"])
        records.append(
            RecordInput(
                startDate=instant,
                endDate=instant,
                creationDate=instant,
                sourceName=SOURCE_NAME,
                type=HK_HEART_RATE,
                value=str(row["bpm"]),
                unit="count/min",
            )
        )
    return records


def map_workout(document: dict[str, Any]) -> WorkoutInput:
    start = _utc(document["start_datetime"])
    end = _utc(document["end_datetime"])
    distance_m = document.get("distance")

    extra_metadata: list[MetadataEntry] = []
    for key in ("label", "intensity", "source"):
        if document.get(key):
            extra_metadata.append(MetadataEntry(key=key, value=str(document[key])))

    calories = document.get("calories")
    return WorkoutInput(
        recordId=str(document["id"]) if document.get("id") else None,
        startDate=start,
        endDate=end,
        creationDate=start,
        sourceName=SOURCE_NAME,
        durationUnit="s",
        duration=(end - start).total_seconds(),
        workoutActivityType=str(document.get("activity", "UNKNOWN")),
        caloriesBurned=float(calories) if calories is not None else None,
        caloriesUnit="kcal" if calories is not None else None,
        distance=float(distance_m) / 1000.0 if distance_m is not None else None,
        distanceUnit="km" if distance_m is not None else None,
        metadataEntry=extra_metadata or None,
    )


# Orchestrator ----------------------------------------------------------------


RECORD_MAPPERS: dict[str, Callable[[dict[str, Any]], list[RecordInput]]] = {
    DATA_TYPE_DAILY_ACTIVITY: map_daily_activity,
    DATA_TYPE_SLEEP: map_sleep,
    DATA_TYPE_DAILY_SPO2: map_daily_spo2,
}

DEFAULT_DATA_TYPES: tuple[str, ...] = (
    DATA_TYPE_DAILY_ACTIVITY,
    DATA_TYPE_SLEEP,
    DATA_TYPE_DAILY_SPO2,
    DATA_TYPE_HEARTRATE,
    DATA_TYPE_WORKOUT,
)


def ingest_documents(
    connection: OuraConnection,
    data_type: str,
    documents: list[dict[str, Any]],
) -> int:
    """Persist already-fetched documents (e.g. from a webhook notification).

    Returns the number of records/workouts written. Unknown data types are
    skipped with a zero count so webhook processing stays forward-compatible
    with data types this version doesn't map yet.
    """
    if data_type == DATA_TYPE_WORKOUT:
        workouts = [map_workout(d) for d in documents]
        ingest_workouts(connection.customer, workouts, source=DataSource.OURA)
        return len(workouts)

    if data_type == DATA_TYPE_HEARTRATE:
        records = map_heartrate(documents)
        ingest_records(connection.customer, records, source=DataSource.OURA)
        return len(records)

    mapper = RECORD_MAPPERS.get(data_type)
    if mapper is None:
        return 0
    records = []
    for document in documents:
        records.extend(mapper(document))
    ingest_records(connection.customer, records, source=DataSource.OURA)
    return len(records)


def sync_user(
    connection: OuraConnection,
    *,
    start: datetime,
    end: datetime,
    data_types: list[str] | None = None,
    client: OuraClient | None = None,
) -> SyncResult:
    """Fetch + ingest all configured data types for ``connection`` over [start, end].

    Document routes filter by calendar day in the user's local timezone, so
    the window's dates are passed as inclusive ``start_date``/``end_date``;
    the heart-rate time series uses the instants directly.

    Oura's recommended architecture is one historical pull like this at
    connect time, then webhook-driven single-document fetches — see
    :mod:`oura.webhooks`.

    Pass a pre-built ``client`` to override the default (useful in tests).
    """
    result = SyncResult()
    owns_client = client is None
    if client is None:
        client = OuraClient(connection)

    try:
        for data_type in data_types or DEFAULT_DATA_TYPES:
            if data_type == DATA_TYPE_HEARTRATE:
                documents = client.list_heartrate(start=start, end=end)
            else:
                documents = client.list_documents(
                    data_type, start_date=start.date(), end_date=end.date()
                )
            result.counts[data_type] = ingest_documents(
                connection, data_type, documents
            )
    finally:
        if owns_client:
            client.close()

    connection.last_sync_at = datetime.now(timezone.utc)
    connection.save(update_fields=["last_sync_at"])
    result.finished_at = datetime.now(timezone.utc)
    return result
