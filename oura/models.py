from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import models


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"
    REVOKED = "revoked", "Revoked"


class OuraConnection(models.Model):
    """Per-user OAuth state for the Oura API v2.

    Health records persist through django-healthdatamodel; this model only
    holds the credentials needed to fetch them. Oura refresh tokens are
    single-use — every refresh rotates them, so both tokens are rewritten
    together.
    """

    customer = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oura_connection",
    )
    # The ``id`` from ``GET /v2/usercollection/personal_info`` (readable with
    # any token, no scope needed): stable across re-consents, and the key
    # webhook notifications carry as ``user_id`` to identify the user.
    oura_user_id = models.CharField(max_length=128, db_index=True)
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()
    scopes = models.JSONField(default=list)
    status = models.CharField(
        max_length=32,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.ACTIVE,
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Oura connection"
        verbose_name_plural = "Oura connections"

    def __str__(self) -> str:
        return f"OuraConnection(customer={self.customer_id}, status={self.status})"

    def is_token_expired(
        self, *, leeway_seconds: int = 60, now: datetime | None = None
    ) -> bool:
        """True if the access token is at or past ``token_expires_at - leeway``.

        The leeway buys time for an in-flight request to complete with the
        same token before Oura's reported expiry (typically 24 hours).
        """
        anchor = now or datetime.now(timezone.utc)
        return anchor >= self.token_expires_at - timedelta(seconds=leeway_seconds)
