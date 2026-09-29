# django-oura

[![CI](https://github.com/django-health/django-oura/actions/workflows/ci.yml/badge.svg)](https://github.com/django-health/django-oura/actions/workflows/ci.yml)

A reusable Django app for the [Oura API v2](https://cloud.ouraring.com/v2/docs). Handles Oura's OAuth 2.0 flow, fetches documents from `api.ouraring.com/v2/usercollection`, receives webhook notifications, and persists everything through [`django-healthdatamodel`](https://github.com/django-health/django-healthdatamodel) so the same storage and query layer serves Apple Health, Fitbit, Google Health, Garmin, and Oura side-by-side.

> The Oura V1 API is sunset, and personal access tokens were retired in December 2025 — this package speaks only V2 with OAuth2. API applications are limited to 10 users before requiring approval from Oura.

## Install

```
pip install django-oura
```

Add both this app and `django-healthdatamodel` to `INSTALLED_APPS`, then run migrations:

```python
INSTALLED_APPS = [
    ...
    "healthdatamodel",
    "oura",
]
```

```
python manage.py migrate
```

The model uses `settings.AUTH_USER_MODEL` so it works with any custom user model.

## Configuration

```python
OURA_CLIENT_ID = "..."  # from https://cloud.ouraring.com/oauth/applications
OURA_CLIENT_SECRET = "..."
OURA_REDIRECT_URI = "https://your-app.example.com/oura/callback/"

# Optional:
OURA_SCOPES = ["daily", "heartrate", "workout", "spo2"]  # the default set
OURA_CONNECT_SUCCESS_URL = "/"  # default "/admin/"
OURA_WEBHOOK_VERIFICATION_TOKEN = "..."  # only for webhooks
```

Register the redirect URI on the Oura application — the token exchange fails on any mismatch. Users consent **per scope** on Oura's authorization page, so the granted set may be narrower than requested; whatever was actually granted is stored on `OuraConnection.scopes`.

## URLs

```python
urlpatterns = [
    ...
    path("oura/", include("oura.urls")),
]
```

This mounts:

- `oura/connect/` — start the OAuth flow (login required)
- `oura/callback/` — the redirect URI target
- `oura/disconnect/` — POST; revokes the token at Oura and marks the connection revoked
- `oura/notifications/` — webhook receiver (GET verification challenge + POST events)

Mobile clients that run the OAuth dance themselves can POST the resulting token dict to a project-local endpoint that calls `oura.oauth.ingest_tokens`.

## Getting data

Oura's recommended architecture is **one historical pull when a user connects, then webhooks** — properly implemented, webhook consumers don't hit the API's 5000-requests-per-5-minutes rate limit.

**Pull.** `oura.ingest.sync_user(connection, start=..., end=...)` fetches and ingests the configured data types. Document routes filter by the calendar day the data belongs to (in the user's local timezone), so re-syncing a window picks up revisions to those days.

```
python manage.py sync_oura                    # last day, all active connections
python manage.py sync_oura --user alice --days 30
python manage.py sync_oura --data-type daily_activity --data-type sleep
```

**Webhooks.** Subscriptions are app-level, one per `(event_type, data_type)` pair, and Oura verifies your endpoint with a GET challenge at subscription time — `oura/notifications/` answers it using `OURA_WEBHOOK_VERIFICATION_TOKEN`. Create them once the endpoint is live:

```
python manage.py create_oura_webhook_subscription \
    --callback-url https://api.example.com/oura/notifications/ \
    --verification-token "$OURA_WEBHOOK_VERIFICATION_TOKEN" \
    --data-type daily_activity --data-type sleep --data-type workout
python manage.py list_oura_webhook_subscriptions
python manage.py delete_oura_webhook_subscription <id>
```

Then connect a signal handler to drive ingest:

```python
from django.dispatch import receiver
from oura.signals import notification_received
from oura.webhooks import process_notification


@receiver(notification_received)
def on_notification(sender, payload, **kwargs):
    process_notification(payload)  # or hand off to celery / a queue
```

Notifications carry `{event_type, data_type, object_id, user_id}`; `process_notification` routes to a user by Oura's stable `user_id`, fetches the referenced document with that user's credentials, and ingests it. The receiver view enforces Oura's `x-oura-signature` HMAC before emitting the signal (set `OURA_WEBHOOK_REQUIRE_SIGNATURE = False` to accept unsigned POSTs). Oura expects a 2xx within 10 seconds — hand off to a queue if your processing is heavy. Subscriptions expire; renew with `oura.webhooks.renew_subscription`.

## What gets stored

This app does **not** define `Record` / `Workout` tables — those live in `django-healthdatamodel`. The `oura.ingest` module maps Oura documents to `healthdatamodel` inputs and persists them with `source="oura"`:

| Oura data type | healthdatamodel records |
| --- | --- |
| `daily_activity` | steps, active calories, basal calories (total − active), equivalent walking distance |
| `sleep` | one sleep-stage record per `sleep_phase_5_min` run (deep/light/REM/awake) |
| `daily_spo2` | daily SpO2 average |
| `heartrate` | one heart-rate sample per 5-minute increment |
| `workout` | one `Workout` per workout |

Oura-specific scores (readiness, sleep score, resilience, stress) have no HealthKit-schema equivalent and are not ingested yet.

The only model defined here is `OuraConnection`: per-user OAuth tokens (Oura refresh tokens are single-use — both tokens are rewritten together on every refresh), granted scopes, connection status, and last sync timestamp.

## Try it without a ring (sandbox mode)

Oura serves generated fake data at `/v2/sandbox/usercollection` — same routes
and shapes, no account or OAuth needed. The demo has first-class support:

```
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
OURA_SANDBOX=1 uv run python manage.py runserver
```

Sign in, click **Sync sandbox data**, and browse the results in the admin. A
placeholder `OuraConnection` is created automatically. Programmatic use:
`OuraClient(connection, sandbox=True)`. Note the sandbox's degenerate corners
(documented in `CLAUDE.md`) before treating it as calibration ground truth.

## Try it on your own data

The repo includes a runnable demo Django project at `demo/`.

### 1. Register an Oura API application (one-time)

Create one at [cloud.ouraring.com/oauth/applications](https://cloud.ouraring.com/oauth/applications) and register the redirect URI `http://localhost:8000/oura/callback/` (exact match, trailing slash included). Unapproved applications are limited to 10 users — fine for the demo.

### 2. Run the demo

```
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
export OURA_CLIENT_ID=...
export OURA_CLIENT_SECRET=...
uv run python manage.py runserver
```

Open <http://localhost:8000/>, sign in with the superuser you just created, then:

- Click **Connect Oura** → consent on Oura (pick your scopes) → land back on the homepage with an `OuraConnection` saved for your user.
- Open the Oura app on your phone so your ring syncs (sleep data only reaches Oura's cloud that way), then pick a window and click **Sync now**.
- Browse the resulting rows at `/admin/healthdatamodel/record/` (and `.../workout/`).

Or drive sync from the terminal:

```
uv run python manage.py sync_oura --user <your-username> --days 30
```

## Documentation

Summaries of the Oura API docs live under `docs/oura/` so they're grep-able offline:

- `authentication.md` — OAuth2 endpoints, scopes, token lifetimes, revocation
- `api.md` — the v2 usercollection routes, params, pagination, payload shapes
- `webhooks.md` — subscription API, verification challenge, signatures, retries

## Development

```
uv sync --group dev
uv run pytest tests/ -v
uv run pre-commit run --all-files
```
