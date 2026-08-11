from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView

from crm.views import AuditedPasswordChangeView, FirstRunAwareLoginView


def health(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/favicon.svg", permanent=True),
        name="favicon",
    ),
    path("admin/", admin.site.urls),
    path("accounts/login/", FirstRunAwareLoginView.as_view(), name="login"),
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
