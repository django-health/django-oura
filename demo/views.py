"""Tiny user-facing views so you can click through the OAuth + sync flow
without bouncing through Django's admin.

* ``home`` — show connection status, link to start OAuth, button to trigger sync.
* ``sync`` — POST handler that runs ``sync_user`` for the requesting user and
  redirects home with a flash message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from oura.client import OuraClient
from oura.ingest import sync_user
from oura.models import OuraConnection


@login_required
def home(request: HttpRequest) -> HttpResponse:
    connection = OuraConnection.objects.filter(customer=request.user).first()

    from healthdatamodel.models import Record, Workout

    record_count = Record.objects.filter(customer=request.user).count()
    workout_count = Workout.objects.filter(customer=request.user).count()

    return render(
        request,
        "demo/home.html",
        {
            "connection": connection,
            "record_count": record_count,
            "workout_count": workout_count,
            "sandbox": settings.OURA_SANDBOX,
        },
    )


@login_required
@require_POST
def sync(request: HttpRequest) -> HttpResponse:
    if settings.OURA_SANDBOX:
        # Sandbox needs no OAuth: any Authorization value works, so a
        # placeholder connection is enough to drive the full sync path.
        connection, _ = OuraConnection.objects.get_or_create(
            customer=request.user,
            defaults={
                "oura_user_id": "sandbox",
                "access_token": "sandbox",
                "refresh_token": "sandbox",
                "token_expires_at": datetime.now(timezone.utc) + timedelta(days=3650),
            },
        )
    else:
        try:
            connection = OuraConnection.objects.get(customer=request.user)
        except OuraConnection.DoesNotExist:
            messages.error(request, "Connect Oura first.")
            return redirect("demo-home")

    days = int(request.POST.get("days", "7"))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    client = OuraClient(connection, sandbox=True) if settings.OURA_SANDBOX else None
    try:
        result = sync_user(connection, start=start, end=end, client=client)
    except Exception as exc:  # noqa: BLE001 — surface anything to the demo user
        messages.error(request, f"Sync failed: {exc}")
        return redirect("demo-home")
    finally:
        if client is not None:
            client.close()

    summary = ", ".join(f"{k}={v}" for k, v in result.counts.items())
    messages.success(request, f"Synced {result.total} record(s): {summary}")
    return redirect("demo-home")
