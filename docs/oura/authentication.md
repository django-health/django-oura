# Oura API v2 — Authentication

Summarized from <https://cloud.ouraring.com/docs/authentication> and the v2
OpenAPI spec (2026-07). The V1 API is sunset; personal access tokens were
deprecated in December 2025 and are no longer available.

## Endpoints

| Purpose | URL |
| --- | --- |
| Authorization (user consent page) | `https://cloud.ouraring.com/oauth/authorize` |
| Token (code + refresh grants, POST form-encoded) | `https://api.ouraring.com/oauth/token` |
| Revocation | `https://api.ouraring.com/oauth/revoke?access_token=...` |

Register applications at <https://cloud.ouraring.com/oauth/applications>.
Unapproved applications are limited to **10 users**; approval from Oura
removes the limit.

## Flows

- **Server-side**: `response_type=code` → exchange the code at the token
  endpoint with `client_id`, `client_secret`, `redirect_uri`. Returns
  `access_token`, `expires_in`, `refresh_token`, `token_type` (and the
  granted `scope`).
- **Client-side (implicit)**: `response_type=token`. 30-day tokens, no
  refresh token. Not used by this package.

No PKCE parameters are documented; the code flow authenticates with the
client secret.

## Scopes

Users consent **per scope** on the authorization page — the granted set can
be narrower than requested.

| Scope | Covers |
| --- | --- |
| `email` | User's email address |
| `personal` | Demographics: age, sex, height, weight |
| `daily` | Daily sleep, activity, readiness (and related) summaries |
| `heartrate` | Time-series heart rate (Gen 3 rings) |
| `workout` | Auto-detected and manual workouts |
| `tag` | User-entered tags |
| `session` | Guided/unguided session data |
| `spo2` | Daily SpO2 averages during sleep |

`GET /v2/usercollection/personal_info` returns its `id` field with **any**
valid token, no scope required — that id is the stable user identifier that
webhook notifications carry as `user_id`.

## Token lifetimes and refresh

- Access tokens expire per `expires_in` (typically 24 hours).
- Refresh tokens are **single-use**: redeeming one invalidates it, and the
  response carries a new refresh token. Persist both tokens together.
- Refresh grant: `grant_type=refresh_token` + `refresh_token` + `client_id`
  + `client_secret`, form-encoded to the token endpoint.

## Common errors

- `401` — missing/expired/revoked token, or malformed `Authorization: Bearer`
  header. Refresh and retry once.
- `403` — token valid but scope not granted, or the user's Oura subscription
  expired.
- `429` — rate limit: 5000 requests per 5-minute period.
