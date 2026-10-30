"""Staff pages for API access: the firm's API switches, tokens, and recent requests."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.services import audit

from .forms import ApiSettingsForm, ApiTokenForm
from .models import ApiRequestLog, ApiSettings, ApiToken

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")
superuser_required = user_passes_test(
    lambda user: user.is_active and user.is_superuser, login_url="login"
)


def can_create_tokens(user, api_settings):
    return user.is_superuser or api_settings.staff_tokens_allowed


@staff_required
@require_GET
def api_access(request):
    api_settings = ApiSettings.load()
    tokens = ApiToken.objects.select_related("user", "revoked_by")
    if not request.user.is_superuser:
        tokens = tokens.filter(user=request.user)
    recent = ApiRequestLog.objects.select_related("token", "user")
    if not request.user.is_superuser:
        recent = recent.filter(token__user=request.user)
    return render(
        request,
        "api/access.html",
        {
            "api_settings": api_settings,
            "settings_form": ApiSettingsForm(instance=api_settings),
            "own_tokens": [token for token in tokens if token.user_id == request.user.pk],
            "other_tokens": [token for token in tokens if token.user_id != request.user.pk],
            "recent_requests": recent[:50],
            "can_create": can_create_tokens(request.user, api_settings),
            "api_url": request.build_absolute_uri(reverse("api_index")),
            "hourly_limit": settings.API_HOURLY_LIMIT,
        },
    )


@superuser_required
@require_POST
def api_settings_update(request):
    form = ApiSettingsForm(request.POST, instance=ApiSettings.load())
    if form.is_valid():
        api_settings = form.save()
        audit(
            request,
            "api_settings.updated",
            detail=(
                f"enabled={api_settings.enabled}, "
                f"staff_tokens_allowed={api_settings.staff_tokens_allowed}"
            ),
        )
        messages.success(request, "The API is on." if api_settings.enabled else "The API is off.")
    return redirect("api_access")


@staff_required
@require_http_methods(["GET", "POST"])
def api_token_create(request):
    if not can_create_tokens(request.user, ApiSettings.load()):
        messages.error(request, "Only superusers can create API tokens on this installation.")
        return redirect("api_access")
    form = ApiTokenForm(request.POST or None)
    if form.is_valid():
        token, raw_token = ApiToken.issue(
            user=request.user,
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
            lifetime_days=form.cleaned_data["lifetime_days"],
        )
        audit(request, "api_token.created", token, token.scope_summary)
        response = render(
            request,
            "api/token_created.html",
            {
                "token": token,
                "raw_token": raw_token,
                "api_url": request.build_absolute_uri(reverse("api_index")),
            },
        )
        response["Cache-Control"] = "no-store"
        return response
    return render(request, "api/token_form.html", {"form": form})


@staff_required
@require_POST
def api_token_revoke(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    if token.user_id != request.user.pk and not request.user.is_superuser:
        raise PermissionDenied
    if not token.revoked_at:
        token.revoked_at = timezone.now()
        token.revoked_by = request.user
        token.save(update_fields=["revoked_at", "revoked_by"])
        audit(request, "api_token.revoked", token, token.name)
        messages.success(request, f"{token.name} was revoked. It stops working immediately.")
    return redirect("api_access")
