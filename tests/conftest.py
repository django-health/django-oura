from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from oura.models import OuraConnection


@pytest.fixture
def customer(db):
    User = get_user_model()
    return User.objects.create_user(username="test-customer")


@pytest.fixture
def connection(customer):
    return OuraConnection.objects.create(
        customer=customer,
        oura_user_id="e4b8f9d2-3c6a-4f7e-9d1b-2a5c8e7f6a3b",
        access_token="oura-initial-access",
        refresh_token="oura-initial-refresh",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        scopes=["daily", "heartrate", "workout", "spo2"],
    )
