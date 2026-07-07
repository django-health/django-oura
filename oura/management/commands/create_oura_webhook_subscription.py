"""Create Oura webhook subscriptions.

Oura requires one subscription per (event_type, data_type) pair; this command
loops over the requested combinations. The receiver endpoint
(``oura/notifications/``) must already be live and reachable — Oura sends the
verification challenge synchronously during creation, and the endpoint
answers it using ``settings.OURA_WEBHOOK_VERIFICATION_TOKEN``.

Example::

    python manage.py create_oura_webhook_subscription \\
        --callback-url https://api.example.com/oura/notifications/ \\
        --verification-token "$OURA_WEBHOOK_VERIFICATION_TOKEN" \\
        --data-type daily_activity --data-type sleep --data-type workout
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from ...webhooks import WebhookError, create_subscription


class Command(BaseCommand):
    help = "Register Oura webhook subscriptions via POST /v2/webhook/subscription."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--callback-url", required=True)
        parser.add_argument(
            "--verification-token",
            required=True,
            help="Must match settings.OURA_WEBHOOK_VERIFICATION_TOKEN on the receiver.",
        )
        parser.add_argument(
            "--data-type",
            dest="data_types",
            action="append",
            required=True,
            help="Data type to subscribe to (e.g. sleep). Repeatable.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_types",
            action="append",
            default=None,
            help="Event type: create, update, or delete. Repeatable. "
            "Default: create and update.",
        )

    def handle(self, *args, **options) -> None:
        event_types = options["event_types"] or ["create", "update"]
        created = []
        for data_type in options["data_types"]:
            for event_type in event_types:
                try:
                    created.append(
                        create_subscription(
                            callback_url=options["callback_url"],
                            verification_token=options["verification_token"],
                            event_type=event_type,
                            data_type=data_type,
                        )
                    )
                except WebhookError as exc:
                    raise CommandError(
                        f"creating ({event_type}, {data_type}) failed: {exc}"
                    ) from exc
        self.stdout.write(json.dumps(created, indent=2))
