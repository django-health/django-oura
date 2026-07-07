# Oura API v2 — usercollection routes

Summarized from the v2 OpenAPI spec, version 1.35 (2026-07). Interactive
reference: <https://cloud.ouraring.com/v2/docs>; the spec JSON lives at
`https://cloud.ouraring.com/v2/static/json/openapi-<version>.json`.

Base URL: `https://api.ouraring.com/v2/usercollection`. All routes are GET
with `Authorization: Bearer <token>`. Rate limit: 5000 requests / 5 minutes.
A parallel `/v2/sandbox/usercollection` tree serves fake data without an Oura
account (same shapes, shared rate limit) — useful for development.

## Route families

**Document routes** — `daily_activity`, `daily_sleep`, `daily_readiness`,
`daily_spo2`, `daily_stress`, `daily_resilience`,
`daily_cardiovascular_age`, `sleep`, `sleep_time`, `workout`, `session`,
`enhanced_tag`, `tag` (deprecated), `rest_mode_period`, `ring_configuration`,
`vO2_max`:

- List: `GET /{type}?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` — dates are
  **inclusive** and interpreted in the **user's local timezone** (they are
  the days the data belongs to, not upload time).
- Detail: `GET /{type}/{document_id}` — e.g. from a webhook `object_id`.
- Response shape: `{"data": [...], "next_token": "..."|null}`. Pass
  `next_token` back (with the same params) until it is null.

**Time-series route** — `heartrate`:

- `GET /heartrate?start_datetime=...&end_datetime=...` (ISO-8601 instants),
  same `{"data", "next_token"}` envelope.
- Rows: `{timestamp, timestamp_unix (ms), bpm, source}` at 5-minute
  increments; `source` ∈ awake/workout/rest/sleep/live/session. Rows have no
  document id.

**Identity** — `GET /personal_info` → `{id, age, weight, height,
biological_sex, email}`. `id` is readable with any token (no scope).

## Payload fields the ingest layer uses

`daily_activity` (scope `daily`): `id`, `day`, `timestamp` (start of the
user's 4am-local activity day), `steps`, `active_calories`,
`total_calories`, `equivalent_walking_distance` (m), `class_5_min`
('0' non-wear … '5' high), `score`, MET fields.

`sleep` (scope `daily`; multiple periods per day): `id`, `day`,
`bedtime_start`/`bedtime_end` (localized), `type`
(deleted/sleep/long_sleep/late_nap/rest), `sleep_phase_5_min` — one char per
5 min from `bedtime_start`: '1' deep, '2' light, '3' REM, '4' awake —
stage durations, `average_heart_rate`, `average_hrv`, `heart_rate`/`hrv`
sample objects (`{interval, items, timestamp}`), `efficiency`, `latency`.

`daily_spo2` (scope `spo2`): `id`, `day`, `spo2_percentage: {average}`,
`breathing_disturbance_index`. Gen 3 rings only. No timestamp — just the
calendar day.

`workout` (scope `workout`): `id`, `day`, `activity`,
`start_datetime`/`end_datetime` (localized), `calories` (kcal), `distance`
(m), `intensity` (easy/moderate/hard), `label`, `source`
(manual/autodetected/confirmed/workout_heart_rate).

## Timestamps

All datetimes are ISO-8601 **with the user's UTC offset** (e.g.
`2026-07-04T23:12:00-04:00`). Parse and convert to UTC — don't strip or
overwrite the offset.

## Data freshness

- Sleep / readiness / sleep-time documents only reach Oura's cloud when the
  user opens the Oura app and syncs their ring.
- Daily activity, daily stress, and heart rate sync periodically in the
  background.
- Oura's recommended pattern: one historical pull at connect time, webhooks
  for everything after.
