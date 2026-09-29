"""HTTP client for the Oura API v2 (``/v2/usercollection``).

Sync-only wrapper around ``httpx.Client``, built from an
:class:`~oura.models.OuraConnection`.

Responsibilities:

* Authorization: inject ``Bearer <access_token>``.
* Token freshness: refresh proactively when the stored expiry is within the
  connection's leeway, and retry once on a 401 to absorb clock skew or external
  token invalidation.
* Resilience: retry 3× with exponential backoff on 429 and 5xx; honor
  ``Retry-After``. (The API allows 5000 requests per 5 minutes.)
* Pagination: multi-document routes return ``{"data": [...], "next_token"}``;
  the list methods follow ``next_token`` transparently.

Two route families share the collection prefix but take different windows:
document routes (``daily_activity``, ``sleep``, ``workout``, …) filter by
``start_date``/``end_date`` — calendar dates, inclusive, interpreted in the
user's local timezone — while the ``heartrate`` time series filters by
``start_datetime``/``end_datetime`` instants.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import httpx

from . import oauth
from .constants import (
    API_BASE_URL,
    SANDBOX_USERCOLLECTION_PATH,
    USERCOLLECTION_PATH,
)

if TYPE_CHECKING:
    from .models import OuraConnection


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


class OuraAPIError(Exception):
    """Non-retryable error returned by the Oura API."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class OuraClient:
    """Thin REST client. Use as a context manager so the underlying httpx
    session is closed deterministically.
    """

    def __init__(
        self,
        connection: OuraConnection,
        *,
        sandbox: bool = False,
        base_url: str = API_BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff_seconds: float = BASE_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """``sandbox=True`` targets ``/v2/sandbox/usercollection`` — Oura's
        generated fake data. The sandbox accepts any non-empty Authorization
        value, so the connection's tokens are sent as-is and never refreshed.
        """
        self.connection = connection
        self._sandbox = sandbox
        collection = SANDBOX_USERCOLLECTION_PATH if sandbox else USERCOLLECTION_PATH
        self._base = f"{base_url.rstrip('/')}/{collection}"
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout)

    # context manager ------------------------------------------------------

    def __enter__(self) -> OuraClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # core request loop ----------------------------------------------------

    def _ensure_fresh_token(self) -> None:
        if self._sandbox:
            return
        if self.connection.is_token_expired():
            oauth.refresh_access_token(self.connection)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.connection.access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self._ensure_fresh_token()
        url = f"{self._base}/{path.lstrip('/')}"
        retried_after_401 = False
        attempt = 0

        while True:
            response = self._http.request(
                method,
                url,
                params=params,
                headers=self._auth_headers(),
            )

            if (
                response.status_code == 401
                and not retried_after_401
                and not self._sandbox
            ):
                # Either clock skew or the token was invalidated externally —
                # force a refresh and retry once.
                oauth.refresh_access_token(self.connection)
                retried_after_401 = True
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._sleep(self._compute_backoff(response, attempt))
                attempt += 1
                continue

            if response.status_code >= 400:
                payload = _safe_json(response)
                message = _extract_error_message(payload, response.text)
                raise OuraAPIError(response.status_code, message, payload)

            if not response.content:
                return None
            return response.json()

    def _compute_backoff(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff * (2**attempt)

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Follow ``next_token`` until exhausted, concatenating ``data``."""
        documents: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            page_params = dict(params)
            if next_token:
                page_params["next_token"] = next_token
            payload = self._request("GET", path, params=page_params) or {}
            documents.extend(payload.get("data") or [])
            next_token = payload.get("next_token")
            if not next_token:
                return documents

    # resource methods -----------------------------------------------------

    def get_personal_info(self) -> dict[str, Any]:
        """``GET personal_info`` — the ``id`` field is readable with any token."""
        return self._request("GET", "personal_info") or {}

    def list_documents(
        self,
        data_type: str,
        *,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """``GET /{data_type}`` over [start_date, end_date], following pagination.

        Dates are inclusive and interpreted in the user's local timezone
        (Oura's semantics for the document routes).
        """
        return self._paginate(
            data_type,
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    def get_document(self, data_type: str, document_id: str) -> dict[str, Any]:
        """``GET /{data_type}/{document_id}`` — a single document, e.g. from a
        webhook notification's ``object_id``.
        """
        return self._request("GET", f"{data_type}/{document_id}") or {}

    def list_heartrate(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """``GET /heartrate`` over [start, end], following pagination.

        The heart-rate route is a time series (5-minute increments) keyed by
        instants, not calendar dates — hence datetime params.
        """
        return self._paginate(
            "heartrate",
            {
                "start_datetime": start.isoformat(),
                "end_datetime": end.isoformat(),
            },
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            if key in payload:
                return str(payload[key])
    return fallback or "(no body)"
