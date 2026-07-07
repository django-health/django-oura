"""List the app's Oura webhook subscriptions (id, types, expiration)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from ...webhooks import WebhookError, list_subscriptions


class Command(BaseCommand):
    help = "List Oura webhook subscriptions via GET /v2/webhook/subscription."

    def handle(self, *args, **options) -> None:
        try:
            subscriptions = list_subscriptions()
        except WebhookError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(subscriptions, indent=2))
