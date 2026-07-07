"""Service URLs, data-type identifiers, and scopes for the Oura API v2.

Endpoint and payload shapes come from Oura's published OpenAPI spec
(``https://cloud.ouraring.com/v2/docs``); local summaries live in
``docs/oura/``. The V1 API is sunset — this package speaks only V2.
"""

# User-facing consent page (GET).
OAUTH_AUTHORIZATION_URL = "https://cloud.ouraring.com/oauth/authorize"
# Token endpoint (POST, form-encoded) for both authorization_code and
# refresh_token grants. Refresh tokens are single-use: every grant returns a
# rotated refresh token and invalidates the old one.
OAUTH_TOKEN_URL = "https://api.ouraring.com/oauth/token"
# Revocation endpoint; takes the access token as a query parameter.
OAUTH_REVOKE_URL = "https://api.ouraring.com/oauth/revoke"

API_BASE_URL = "https://api.ouraring.com"
USERCOLLECTION_PATH = "v2/usercollection"
WEBHOOK_SUBSCRIPTION_PATH = "v2/webhook/subscription"

# Data types, as they appear in both REST paths
# (``/v2/usercollection/{type}``) and webhook notification ``data_type``
# fields. ``heartrate`` is REST-only — it is a time-series route with
# ``start_datetime``/``end_datetime`` params and is not a webhook data type.
DATA_TYPE_DAILY_ACTIVITY = "daily_activity"
DATA_TYPE_DAILY_READINESS = "daily_readiness"
DATA_TYPE_DAILY_SLEEP = "daily_sleep"
DATA_TYPE_DAILY_SPO2 = "daily_spo2"
DATA_TYPE_DAILY_STRESS = "daily_stress"
DATA_TYPE_ENHANCED_TAG = "enhanced_tag"
DATA_TYPE_HEARTRATE = "heartrate"
DATA_TYPE_SESSION = "session"
DATA_TYPE_SLEEP = "sleep"
DATA_TYPE_SLEEP_TIME = "sleep_time"
DATA_TYPE_WORKOUT = "workout"

# OAuth scopes. Users consent per-scope; a token only reaches the routes its
# scopes cover. ``personal_info``'s ``id`` field is readable with ANY valid
# token (no scope required), which is how connections learn their stable
# Oura user id for webhook routing.
SCOPE_EMAIL = "email"
SCOPE_PERSONAL = "personal"
SCOPE_DAILY = "daily"
SCOPE_HEARTRATE = "heartrate"
SCOPE_WORKOUT = "workout"
SCOPE_TAG = "tag"
SCOPE_SESSION = "session"
SCOPE_SPO2 = "spo2"

ALL_SCOPES = (
    SCOPE_EMAIL,
    SCOPE_PERSONAL,
    SCOPE_DAILY,
    SCOPE_HEARTRATE,
    SCOPE_WORKOUT,
    SCOPE_TAG,
    SCOPE_SESSION,
    SCOPE_SPO2,
)

# What the default sync ingests: daily summaries (daily_activity, sleep via
# the detailed sleep route, daily_spo2), the heart-rate time series, and
# workouts.
DEFAULT_SCOPES = (SCOPE_DAILY, SCOPE_HEARTRATE, SCOPE_WORKOUT, SCOPE_SPO2)

# The API is limited to 5000 requests per 5-minute period. Oura's recommended
# architecture is webhooks: one historical pull at connect time, then
# notification-driven fetches of single documents.
RATE_LIMIT_REQUESTS = 5000
RATE_LIMIT_WINDOW_SECONDS = 300

# Human-readable, stored in Record.sourceName. The machine identifier is
# ``healthdatamodel.constants.DataSource.OURA`` (added in 0.7.0).
SOURCE_NAME = "Oura"
