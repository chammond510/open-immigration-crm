from django.conf import settings
from django.core.checks import Error, Warning, register


@register(deploy=True)
def production_configuration_check(app_configs, **kwargs):
    findings = []
    if settings.DEBUG:
        findings.append(
            Error(
                "DEBUG must be false in production.",
                id="openimmigration.E001",
            )
        )
    if not settings.SESSION_COOKIE_SECURE or not settings.CSRF_COOKIE_SECURE:
        findings.append(
            Error(
                "Secure session and CSRF cookies are required in production.",
                id="openimmigration.E002",
            )
        )
    if not settings.CSRF_TRUSTED_ORIGINS:
        findings.append(
            Warning(
                "Set CSRF_TRUSTED_ORIGINS to the public HTTPS origin.",
                id="openimmigration.W001",
            )
        )
    if settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"):
        findings.append(
            Warning(
                "SQLite is intended for evaluation only; use PostgreSQL in production.",
                id="openimmigration.W002",
            )
        )
    return findings
