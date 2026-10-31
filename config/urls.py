from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView

from crm.connect import sign_in
from crm.connect.views import receive_webhook
from crm.mcp import views as mcp_views
from crm.team import views as team_views
from crm.views import (
    AuditedPasswordChangeView,
    FirstRunAwareLoginView,
    two_factor_disable,
    two_factor_recovery_codes,
    two_factor_settings,
    two_factor_verify,
)


def health(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    # MCP server and its sign-in; see docs/MCP.md.
    path("mcp", mcp_views.endpoint, name="mcp"),
    path("", include("crm.mcp.oauth_urls")),
    path("settings/mcp/", include("crm.mcp.urls")),
    path("settings/ai/", include("crm.ai.urls")),
    path("ai/", include("crm.ai.feature_urls")),
    # Provider webhooks; the token is a per-connection secret. See docs/DEPLOYMENT.md.
    path("hooks/<str:token>/", receive_webhook, name="connector_webhook"),
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/favicon.svg", permanent=True),
        name="favicon",
    ),
    # The admin's own login form would skip two-factor sign-in, so send it to ours.
    path("admin/login/", RedirectView.as_view(pattern_name="login", query_string=True)),
    path("admin/", admin.site.urls),
    path("connections/", include("crm.connect.urls")),
    path("events/", include("crm.events.urls")),
    path("workflows/", include("crm.workflows.urls")),
    path("reports/", include("crm.reports.urls")),
    path("settings/custom-fields/", include("crm.customfields.urls")),
    path("translations/", include("crm.translations.urls")),
    path("imports/", include("crm.imports.urls")),
    path("shipments/", include("crm.shipments.urls")),
    path("communications/", include("crm.comms.urls")),
    path("billing/", include("crm.billing.urls")),
    path("notices/", include("crm.notices.urls")),
    path("research/", include("crm.research.urls")),
    path("signatures/", include("crm.signatures.urls")),
    # Token-authenticated JSON API; see docs/API.md.
    path("api/", include("crm.api.urls")),
    path("settings/api/", include("crm.api.page_urls")),
    path("settings/team/", include("crm.team.urls")),
    path("notifications/", include("crm.notifications.urls")),
    path("accounts/profile/", team_views.profile, name="profile"),
    # Invitation and password reset links; see crm/team.
    path("accounts/welcome/done/", team_views.password_saved, name="welcome_done"),
    path("accounts/password-reset/", team_views.password_reset, name="password_reset"),
    path(
        "accounts/password-reset/sent/",
        team_views.password_reset_sent,
        name="password_reset_sent",
    ),
    path(
        "accounts/password-reset/done/",
        team_views.password_saved,
        name="password_reset_complete",
    ),
    path(
        "accounts/password-reset/<uidb64>/<token>/",
        team_views.ResetPasswordView.as_view(),
        name="password_reset_confirm",
    ),
    path("accounts/welcome/<uidb64>/<token>/", team_views.WelcomeView.as_view(), name="welcome"),
    path("accounts/login/", FirstRunAwareLoginView.as_view(), name="login"),
    path("accounts/login/verify/", two_factor_verify, name="two_factor_verify"),
    path("accounts/sign-in/callback/", sign_in.callback, name="staff_sign_in_callback"),
    path("accounts/sign-in/<uuid:pk>/", sign_in.start, name="staff_sign_in"),
    path("accounts/two-factor/", two_factor_settings, name="two_factor_settings"),
    path(
        "accounts/two-factor/recovery-codes/",
        two_factor_recovery_codes,
        name="two_factor_recovery_codes",
    ),
    path("accounts/two-factor/disable/", two_factor_disable, name="two_factor_disable"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password-change/",
        AuditedPasswordChangeView.as_view(),
        name="password_change",
    ),
    path(
        "accounts/password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html"
        ),
        name="password_change_done",
    ),
    path("", include("crm.urls")),
]
