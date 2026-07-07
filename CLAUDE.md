# Notes for Claude (and human contributors)

This project is a reusable Django library — small, focused, with a real
shipping cadence to PyPI. Slice-sized changes; commit messages with the
"why"; tests for every behavior; live calibration over docs guessing.

It is a sibling of `django-health/django-google-health` and
`django-health/django-garmin` and deliberately mirrors their layout,
pyproject/CI/pre-commit setup, and OAuth/client/ingest module boundaries.
When in doubt about a pattern, check what those repos do.

## Oura API ground truth

The Oura V2 OpenAPI spec (`https://cloud.ouraring.com/v2/docs`, spec JSON at
`/v2/static/json/openapi-<version>.json`) is ground truth for routes and
payload shapes; `docs/oura/` summarizes it plus the authentication and
webhook guides. Payload shapes in `oura/ingest.py` and the test fixtures were
written against spec version 1.35 and have NOT yet been calibrated against a
live ring — when live credentials are available, verify against real
payloads and promote findings into code comments + tests, not just commit
messages.

Live-calibration entry point: run the demo (`README.md` → "Try it on your own
data"), then pull the access token from `db.sqlite3` and hit
`https://api.ouraring.com/v2/usercollection/...` directly with `httpx` before
guessing at code fixes.

Partial calibration was done 2026-07-07 against `/v2/sandbox/usercollection`
(needs any non-empty Authorization header, nothing else). All five default
data types fetched and ingested cleanly. Known sandbox degeneracies — good
for smoke tests, do NOT calibrate timing/stage logic against them:

- Sleep documents have `bedtime_start == bedtime_end` (midnight) and
  `sleep_phase_5_min: null`; the real `total_sleep_duration` is what drove
  the duration-anchored fallback in `map_sleep`.
- Workouts have `start_datetime == end_datetime` (zero duration).
- Heart-rate rows carry a `producer_timestamp` field not in spec 1.35, and
  timestamps use a `Z` suffix while document routes use `.000+00:00` —
  `_utc()` handles both.

Still uncalibrated against real-ring data: `sleep_phase_5_min` run timing,
the webhook HMAC signature scheme, and per-scope consent behavior.

Things Oura does differently from its siblings (don't "fix" them):

- Users consent PER SCOPE on the authorization page; the granted set can be
  narrower than requested and only shows up on the token response.
- Refresh tokens are SINGLE-USE and rotate on every refresh — both tokens
  persist together (like Garmin, unlike Google).
- Document routes filter by the calendar day the data belongs to, in the
  user's LOCAL timezone (`start_date`/`end_date`, inclusive) — not upload
  time. `heartrate` is the odd one out: a time series filtered by
  `start_datetime`/`end_datetime`.
- Timestamps are localized (`2026-07-04T23:12:00-04:00`). Never feed them to
  `RecordInput`'s string coercion — it assumes naive-UTC. `oura.ingest._utc`
  parses and converts properly.
- Webhook subscriptions are app-level (`x-client-id`/`x-client-secret`
  headers), one per (event_type, data_type) pair, with a synchronous GET
  challenge at creation and periodic expiry/renewal.
- Webhook notifications carry only ids — the document is fetched with the
  matching user's token. The HMAC signature scheme in
  `oura.webhooks.compute_signature` (HMAC-SHA256 over `timestamp + body`,
  uppercase hex) is from Oura's docs example and is a prime candidate for
  live calibration.
- Sleep stages come as a `sleep_phase_5_min` digit string, not intervals;
  the mapper collapses runs and clamps the tail to `bedtime_end`.

## Upstream contributions to django-healthdatamodel

The sister repo `django-health/django-healthdatamodel` is where the storage
layer lives. When a change there unblocks this project, you have end-to-end
release autonomy as long as CI is green:

1. Open the PR.
2. Wait for CI green.
3. Bump version in `pyproject.toml` (minor for additive, patch for fix).
4. Squash-merge.
5. Tag `v<X>` on `main`, push the tag — triggers PyPI publish via OIDC.
6. Bump the floor in this repo's `pyproject.toml`, swap any local stopgaps
   for the upstream API, commit + push.

Both repos publish to PyPI via trusted publishing — no manual upload.
Heads-up from the v0.7.0 release: `makemigrations` output is not
ruff-formatted — run `pre-commit run --all-files` before committing a
generated migration or the Pre-commit workflow goes red.

## Out of scope for this project

- **Token encryption at rest.** Production deployment uses Postgres with
  encryption-at-rest at the storage layer. Don't add Fernet /
  django-cryptography fields to `OuraConnection`.
- **Signup view in the demo.** `createsuperuser` is the way. The demo exists
  so the maintainer can test against their own Oura account; it isn't a
  hosted SaaS shell.
- **Oura-specific scores (readiness, resilience, stress).** They have no
  HealthKit-schema equivalent in healthdatamodel today. If they're ever
  wanted, that's an upstream schema conversation first, not a local hack.
- **The legacy wellrider Oura V1 code.** It was removed upstream
  (massmutual wellrider PR #1157) and targeted the sunset V1 API; it is not
  a compatibility target. Its one useful legacy: basal calories = `total −
  active`, which this package preserves.
