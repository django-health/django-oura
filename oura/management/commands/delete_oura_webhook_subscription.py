"""Delete an Oura webhook subscription by id (see list_oura_webhook_subscriptions)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ...webhooks import WebhookError, delete_subscription


class Command(BaseCommand):
    help = (
        "Delete an Oura webhook subscription via DELETE /v2/webhook/subscription/{id}."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("subscription_id")

    def handle(self, *args, **options) -> None:
        try:
            delete_subscription(options["subscription_id"])
        except WebhookError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Deleted subscription {options['subscription_id']}.")
