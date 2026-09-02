"""
Django settings for the checkcalc project.

The project is intentionally small: a SQLite database and a rich Django admin
interface for building and settling shared checks (restaurant bills, group
purchases, and similar).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    """Read a boolean flag from the environment ("1", "true", "yes", "on")."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=()):
    value = os.environ.get(name)
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


# SECURITY WARNING: keep the secret key used in production secret!
# The fallback below only exists so the project runs out of the box in
# development; set DJANGO_SECRET_KEY for any real deployment.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-before-deploying",
)

DEBUG = env_bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "checks",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "checkcalc.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "checkcalc.wsgi.application"
ASGI_APPLICATION = "checkcalc.asgi.application"


# Database — SQLite, stored next to manage.py.
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3")),
        "OPTIONS": {
            # Keep concurrent admin sessions from tripping over each other.
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded receipt photos.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))
# Receipt photos are big; keep them out of memory and off the request path.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_REDIRECT_URL = "/admin/"

# The currency symbol the admin uses when rendering money columns.
CURRENCY_SYMBOL = os.environ.get("CHECKCALC_CURRENCY_SYMBOL", "$")


# Receipt parsing
# ---------------
# Which model reads uploaded receipts: "gemini", "ollama", "claude", or "auto"
# to take the first one that is configured (Gemini, then Claude, then Ollama —
# which needs no key at all).
RECEIPT_PARSER_BACKEND = os.environ.get("RECEIPT_PARSER_BACKEND", "auto")
RECEIPT_PARSER_TIMEOUT = float(os.environ.get("RECEIPT_PARSER_TIMEOUT", "120"))

# Gemini — free tier, no card required. Key from aistudio.google.com/apikey.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
RECEIPT_GEMINI_MODEL = os.environ.get("RECEIPT_GEMINI_MODEL", "gemini-2.5-flash")
# Override only to route through a gateway or a regional endpoint.
RECEIPT_GEMINI_ENDPOINT = os.environ.get("RECEIPT_GEMINI_ENDPOINT", "")

# Ollama — a model on your own machine. Free and offline; needs a vision model
# pulled (`ollama pull llama3.2-vision`) for photos and scans.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
RECEIPT_OLLAMA_MODEL = os.environ.get("RECEIPT_OLLAMA_MODEL", "llama3.2-vision")

# Claude — paid. The key may be left unset: the SDK also picks up
# ANTHROPIC_AUTH_TOKEN or a profile written by `ant auth login`.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RECEIPT_CLAUDE_MODEL = os.environ.get("RECEIPT_CLAUDE_MODEL", "claude-opus-5")

# Read a receipt as soon as it is uploaded, and build a draft check from it.
# Turn these off to upload now and parse later from the admin actions.
RECEIPT_PARSE_ON_UPLOAD = env_bool("RECEIPT_PARSE_ON_UPLOAD", default=True)
RECEIPT_CREATE_CHECK_ON_PARSE = env_bool("RECEIPT_CREATE_CHECK_ON_PARSE", default=True)
