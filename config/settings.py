"""Settings for Open Immigration CRM.

The development defaults are intentionally local and inert. Production must
provide its own secret, hosts, database, and TLS-facing configuration.
"""

import base64
import binascii
import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Development mode is opt-in: without configuration the application refuses to
# start rather than serving client data with debug defaults.
DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.environ.get("SECRET_KEY", "development-only-key-open-immigration-crm")

if not DEBUG and (
    not SECRET_KEY
    or SECRET_KEY == "development-only-key-open-immigration-crm"
    or len(SECRET_KEY) < 50
):
    raise ImproperlyConfigured(
        "Production requires SECRET_KEY with at least 50 characters. "
        "For local development, set DEBUG=true to use the inert development key."
    )

# Field-level encryption for stored secrets such as two-factor keys. The first
# key encrypts and every listed key can decrypt. To rotate, put a new key first,
# run `python manage.py rotate_field_encryption`, then remove the old key.
DEVELOPMENT_FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(
    b"development-only-field-key-00000"
).decode()
FIELD_ENCRYPTION_KEYS = [
    key.strip() for key in os.environ.get("FIELD_ENCRYPTION_KEYS", "").split(",") if key.strip()
]
if DEBUG and not FIELD_ENCRYPTION_KEYS:
    FIELD_ENCRYPTION_KEYS = [DEVELOPMENT_FIELD_ENCRYPTION_KEY]
if not DEBUG and (
    not FIELD_ENCRYPTION_KEYS or DEVELOPMENT_FIELD_ENCRYPTION_KEY in FIELD_ENCRYPTION_KEYS
):
    raise ImproperlyConfigured(
        "Production requires FIELD_ENCRYPTION_KEYS. Generate a key with: python3 -c "
        '"import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
    )
for _key in FIELD_ENCRYPTION_KEYS:
    try:
        _decoded = base64.urlsafe_b64decode(_key.encode())
    except (binascii.Error, ValueError):
        _decoded = b""
    if len(_decoded) != 32:
        raise ImproperlyConfigured(
            "Each FIELD_ENCRYPTION_KEYS value must be 32 random bytes in URL-safe base64."
        )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Production requires at least one ALLOWED_HOSTS value.")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "django_tasks",
    "django_tasks_db",
    "crm.apps.CrmConfig",
    "crm.connect.apps.ConnectConfig",
    "crm.comms.apps.CommsConfig",
    "crm.billing.apps.BillingConfig",
    "crm.notices.apps.NoticesConfig",
    "crm.api.apps.ApiConfig",
    "crm.events.apps.EventsConfig",
    "crm.workflows.apps.WorkflowsConfig",
    "crm.customfields.apps.CustomFieldsConfig",
    "crm.reports.apps.ReportsConfig",
    "oauth2_provider",
    "crm.mcp.apps.McpConfig",
    "crm.ai.apps.AiConfig",
    "crm.research.apps.ResearchConfig",
    "crm.signatures.apps.SignaturesConfig",
    "crm.imports.apps.ImportsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "crm.middleware.TwoFactorMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "crm.middleware.SecurityHeadersMiddleware",
    "axes.middleware.AxesMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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
                "crm.context_processors.firm_profile",
                "crm.context_processors.running_timer",
                "crm.ai.context_processors.ai_features",
            ],
        },
    }
]

database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url:
    DATABASES = {
        "default": dj_database_url.parse(
            database_url,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
elif os.environ.get("POSTGRES_HOST", "").strip():
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "opencrm"),
            "USER": os.environ.get("POSTGRES_USER", "opencrm"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Lock a username+address pair after repeated sign-in failures. Operators can
# tune the limits or clear a lockout with `python manage.py axes_reset`.
AXES_ENABLED = env_bool("AXES_ENABLED", True)
AXES_FAILURE_LIMIT = int(os.environ.get("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = timedelta(minutes=int(os.environ.get("AXES_COOLOFF_MINUTES", "15")))
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "registration/locked_out.html"
AXES_CLIENT_IP_CALLABLE = "crm.services.client_ip"

# Two-factor sign-in. Repeated incorrect codes lock the second step for the
# same cool-off period as a password lockout.
TWO_FACTOR_FAILURE_LIMIT = int(os.environ.get("TWO_FACTOR_FAILURE_LIMIT", "5"))
TWO_FACTOR_LOGIN_TIMEOUT = timedelta(minutes=5)

# Optional connectors. Every module in these packages is imported at startup so
# its connectors can register; installed packages can add more through the
# "open_immigration_crm.connectors" entry point. See connectors/README.md.
CONNECTOR_PACKAGES = ["connectors"]
CONNECTOR_CATALOG = BASE_DIR / "connectors" / "catalog.toml"
# Turning this off blocks every outbound connector action without a deploy.
CONNECTORS_OUTBOUND_ENABLED = env_bool("CONNECTORS_OUTBOUND_ENABLED", True)
CONNECTORS_OUTBOUND_HOURLY_LIMIT = int(os.environ.get("CONNECTORS_OUTBOUND_HOURLY_LIMIT", "200"))
# Hidden connectors (the fictional examples) appear in the catalog for testing.
CONNECTORS_SHOW_HIDDEN = env_bool("CONNECTORS_SHOW_HIDDEN", DEBUG)
# Beta connectors offered only behind a flag, by flag name, such as "chatgpt_plan".
CONNECTOR_FLAGS = frozenset(
    flag.strip() for flag in os.environ.get("CONNECTOR_FLAGS", "").split(",") if flag.strip()
)
CONNECTOR_WEBHOOK_MAX_BYTES = int(os.environ.get("CONNECTOR_WEBHOOK_MAX_BYTES", str(1024 * 1024)))

# REST API (/api/v1/). It stays off until a superuser turns it on under API access.
API_HOURLY_LIMIT = int(os.environ.get("API_HOURLY_LIMIT", "1000"))

# MCP server (/mcp) for Claude and ChatGPT, with django-oauth-toolkit as its
# authorization server. It stays off until a superuser turns it on under AI
# assistants. See docs/MCP.md.
MCP_HOURLY_LIMIT = int(os.environ.get("MCP_HOURLY_LIMIT", "1000"))
# Phone numbers written without a country code are read as this region's (ISO 3166 code).
PHONE_DEFAULT_REGION = os.environ.get("PHONE_DEFAULT_REGION", "US").upper()
OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth2_provider.Application"
OAUTH2_PROVIDER = {
    "SCOPES": {
        "crm:read": "Read contacts, matters, work, notices, communications, and time",
        "crm:write": "Add tasks, notes, communications, and status checks, if the firm allows it",
    },
    "DEFAULT_SCOPES": ["crm:read", "crm:write"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 3600,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "ROTATE_REFRESH_TOKEN": True,
    # A reused refresh token revokes its whole family. No grace period, because
    # tokens are stored only as hashes and an old one could not be returned again.
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "PKCE_REQUIRED": True,
    # Ask every time; a client registered by anyone cannot reuse an earlier approval.
    "REQUEST_APPROVAL_PROMPT": "force",
    # Claude and ChatGPT use HTTPS callbacks. Plain-http loopback (Claude Code) stays off.
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https"],
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": [
        "none",
        "client_secret_post",
        "client_secret_basic",
    ],
    "DCR_ENABLED": True,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("crm.mcp.oauth.RegistrationPermission",),
    "CIMD_ENABLED": True,
    "CIMD_REGISTRATION_PERMISSION_CLASSES": ("crm.mcp.oauth.MetadataDocumentPermission",),
    "OAUTH2_PROTECTED_RESOURCE_NAME": "Open Immigration CRM",
    "OAUTH2_PROTECTED_RESOURCE_DOCUMENTATION": (
        "https://github.com/chammond510/open-immigration-crm/blob/main/docs/MCP.md"
    ),
    # RFC 9700 (OAuth security best practice): hashed tokens, S256-only PKCE, the
    # RFC 9207 iss parameter, no implicit or password grants, header-only tokens.
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
    "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
}

# Background work runs in `python manage.py run_worker`, which keeps its queue in
# the application database. No Redis or message broker is involved.
TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}
WORKER_TICK_SECONDS = int(os.environ.get("WORKER_TICK_SECONDS", "60"))

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/protected-media/"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
DOCUMENT_UPLOAD_MAX_BYTES = int(os.environ.get("DOCUMENT_UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)

# Trust X-Forwarded-Proto only when a TLS-terminating proxy is declared;
# trusting it on a directly exposed server lets clients spoof "https".
if env_bool("TRUST_X_FORWARDED_PROTO", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
