"""Pydantic models for Oura OAuth request/response payloads."""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, field_validator


class OAuthFlowState(BaseModel):
    """Per-request state stashed in the Django session between ``connect`` and ``callback``.

    ``state`` defends against CSRF. Oura's flow is plain OAuth2
    authorization-code (no PKCE), so state is the only thing to round-trip.
    """

    state: str


class OuraTokens(BaseModel):
    """Token-endpoint response from ``api.ouraring.com/oauth/token``.

    Mirrors the JSON Oura returns for both ``authorization_code`` and
    ``refresh_token`` grants. Refresh tokens are single-use: each response
    carries a rotated ``refresh_token`` and the previous one is invalidated.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    access_token: str
    expires_in: int
    token_type: str = "bearer"
    scope: str = ""
    refresh_token: str

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_scope(cls, value: object) -> str:
        # Oura returns a space-separated string (e.g. "daily heartrate");
        # accept a pre-parsed list too.
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return " ".join(str(v) for v in value)
        return str(value)

    @property
    def scopes(self) -> list[str]:
        return self.scope.split() if self.scope else []

    def expires_at(self, *, now: datetime | None = None) -> datetime:
        anchor = now or datetime.now(timezone.utc)
        return anchor + timedelta(seconds=self.expires_in)
