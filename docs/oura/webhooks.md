# Oura API v2 — Webhooks

Summarized from the "Webhook Subscription Routes" section of the v2 OpenAPI
spec (1.35, 2026-07). Webhooks are Oura's recommended way to consume data:
one historical pull at connect time, then notification-driven fetches.
Notifications arrive ~30 seconds after a user's ring syncs.

## Subscription API

Base: `https://api.ouraring.com/v2/webhook/subscription`. Authenticated with
**application** credentials via headers (not user tokens):

```
x-client-id: <client id>
x-client-secret: <client secret>
```

| Operation | Route |
| --- | --- |
| Create | `POST /v2/webhook/subscription` |
| List | `GET /v2/webhook/subscription` |
| Get / Update | `GET`/`PUT /v2/webhook/subscription/{id}` |
| Renew | `PUT /v2/webhook/subscription/renew/{id}` |
| Delete | `DELETE /v2/webhook/subscription/{id}` |

Create body — one subscription per `(event_type, data_type)` pair:

```json
{
  "callback_url": "https://your-server.example.com/oura/notifications/",
  "verification_token": "your-secret-verification-token",
  "event_type": "create",       // create | update | delete
  "data_type": "sleep"
}
```

`data_type` ∈ tag, enhanced_tag, workout, session, sleep, daily_sleep,
daily_readiness, daily_activity, daily_spo2, sleep_time, rest_mode_period,
ring_configuration, daily_stress, daily_cardiovascular_age, daily_resilience,
vo2_max, meal. (`heartrate` is not a webhook data type.)

Subscription objects carry an `expiration_time`; renew before it passes or
the subscription lapses.

## Verification challenge (at creation)

Oura synchronously GETs the callback URL during `POST /subscription`:

```
GET {callback_url}?verification_token=<your token>&challenge=<random>
```

The endpoint must confirm the token matches and answer within 10 seconds:

```json
{"challenge": "<random>"}
```

## Event notifications

```
POST {callback_url}
x-oura-signature: <uppercase hex HMAC>
x-oura-timestamp: <unix timestamp>

{
  "event_type": "update",
  "data_type": "sleep",
  "object_id": "12345abc",
  "event_time": "2026-07-04T08:00:00+00:00",
  "user_id": "<stable oura user id>"
}
```

- The notification carries only ids: fetch the document with the matching
  user's token via `GET /v2/usercollection/{data_type}/{object_id}`.
- `user_id` equals the `id` from that user's `personal_info`.
- Signature: `HMAC-SHA256(client_secret, timestamp + body)`, uppercase hex.
  (Oura's docs show the body as the JSON payload re-serialized; verify
  against the raw request body first when calibrating live.)
- Respond 2xx within 10 seconds; process heavy work asynchronously.

## Failure handling

- Oura retries failed deliveries ~10 times (4xx, 5xx, and timeouts alike),
  for roughly an hour.
- Responding **410 Gone** cancels the subscription — don't return 410 for
  transient errors.
- After extended downtime, reconcile by pulling the missed window
  (`manage.py sync_oura --days N`).
