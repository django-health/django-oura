"""Minimal Django settings for pytest-django."""

SECRET_KEY = "test-secret-key"  # noqa: S105
DEBUG = False

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.messages",
    "django.contrib.sessions",
    "healthdatamodel",
    "oura",
    "demo",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "tests.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

OURA_CLIENT_ID = "test-client-id"
OURA_CLIENT_SECRET = "test-client-secret"  # noqa: S105
OURA_REDIRECT_URI = "http://testserver/oura/callback/"
OURA_WEBHOOK_VERIFICATION_TOKEN = "test-verification-token"  # noqa: S105
OURA_SANDBOX = False
