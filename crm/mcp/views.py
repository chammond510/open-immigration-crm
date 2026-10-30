import hashlib
from collections import OrderedDict
from urllib.parse import urlsplit

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from oauth2_provider.models import AccessToken, Application, RefreshToken

from crm.services import audit
from crm.two_factor import confirmed_device, firm_requires_two_factor

from .models import McpSettings, McpToolCall
from .oauth import resource_metadata_url, resource_url
from .protocol import error_response, handle

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")
MAX_BODY_BYTES = 1024 * 1024


def challenge(request, error=""):
    """A 401 that tells the assistant where to start signing in (RFC 9728)."""
    response = JsonResponse(
        {"error": error or "unauthorized", "error_description": "Sign in to use this server."},
        status=401,
    )
    parts = [f'resource_metadata="{resource_metadata_url(request)}"', 'scope="crm:read crm:write"']
    if error:
        parts.insert(0, f'error="{error}"')
    response["WWW-Authenticate"] = "Bearer " + ", ".join(parts)
    return response


def access_token(request):
    """The valid access token on the request, or a string naming what is wrong."""
    scheme, _, raw = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        return ""
    checksum = hashlib.sha256(raw.strip().encode()).hexdigest()
    token = (
        AccessToken.objects.select_related("user", "application")
        .filter(token_checksum=checksum)
        .first()
    )
    if token is None or token.is_expired() or not token.allows_audience(resource_url(request)):
        return "invalid_token"
    # Registration tokens from dynamic client registration carry no CRM scope.
    if "crm:read" not in token.scope.split():
        return "invalid_token"
    user = token.user
    if user is None or not (user.is_active and user.is_staff):
        return "invalid_token"
    if firm_requires_two_factor() and confirmed_device(user) is None:
        return "invalid_token"
    return token


def origin_allowed(request):
    """Browsers send Origin; assistants calling from their own servers usually do not."""
    origin = request.headers.get("Origin")
    return not origin or urlsplit(origin).netloc == request.get_host()


@csrf_exempt
def endpoint(request):
    """The MCP endpoint: POST only, one JSON response per message."""
    if request.method != "POST":
        response = JsonResponse({"error": "Use POST."}, status=405)
        response["Allow"] = "POST"
        return response
    if not origin_allowed(request):
        return error_response(403, -32600, "Forbidden origin")
    mcp_settings = McpSettings.load()
    if not mcp_settings.enabled:
        return error_response(403, -32600, "The CRM's MCP server is turned off.")
    token = access_token(request)
    if not token:
        return challenge(request)
    if isinstance(token, str):
        return challenge(request, token)
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
        return error_response(413, -32600, "The request is too large.")
    request.user = token.user
    response = handle(request, token, mcp_settings)
    response["Cache-Control"] = "no-store"
    return response


class McpSettingsForm(forms.ModelForm):
    class Meta:
        model = McpSettings
        fields = ["enabled", "write_tools_enabled"]


def connections_for(refresh_tokens):
    """Live refresh tokens grouped into one row per person and assistant."""
    rows = OrderedDict()
    for token in refresh_tokens.select_related("application", "user").order_by("-created"):
        key = (token.user_id, token.application_id)
        row = rows.setdefault(
            key,
            {
                "user": token.user,
                "application": token.application,
                "since": token.created,
                "last_refreshed": token.created,
            },
        )
        row["since"] = min(row["since"], token.created)
    for row in rows.values():
        app = row["application"]
        uri = (app.redirect_uris or "").split()
        row["callback_host"] = urlsplit(uri[0]).hostname if uri else ""
        row["registered_by"] = app.get_registration_source_display() if app else ""
    return list(rows.values())


@staff_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    mcp_settings = McpSettings.load()
    form = McpSettingsForm(request.POST or None, instance=mcp_settings)
    if request.method == "POST":
        if not request.user.is_superuser:
            raise PermissionDenied
        if form.is_valid():
            mcp_settings = form.save()
            audit(
                request,
                "mcp_settings.updated",
                detail=f"enabled={mcp_settings.enabled}, "
                f"write_tools={mcp_settings.write_tools_enabled}",
            )
            messages.success(
                request,
                "The MCP server is on." if mcp_settings.enabled else "The MCP server is off.",
            )
        return redirect("mcp_settings")
    live = RefreshToken.objects.filter(revoked__isnull=True, user__isnull=False)
    calls = McpToolCall.objects.select_related("user", "application")
    if not request.user.is_superuser:
        calls = calls.filter(user=request.user)
    return render(
        request,
        "mcp/settings.html",
        {
            "mcp_settings": mcp_settings,
            "form": form,
            "connector_url": resource_url(request),
            "own_connections": connections_for(live.filter(user=request.user)),
            "other_connections": connections_for(live.exclude(user=request.user))
            if request.user.is_superuser
            else [],
            "recent_calls": calls[:50],
        },
    )


@staff_required
@require_POST
def disconnect(request, application_pk, user_pk):
    if user_pk != request.user.pk and not request.user.is_superuser:
        raise PermissionDenied
    application = Application.objects.filter(pk=application_pk).first()
    if application:
        for token in RefreshToken.objects.filter(
            application=application, user_id=user_pk, revoked__isnull=True
        ):
            token.revoke()
        AccessToken.objects.filter(application=application, user_id=user_pk).delete()
        audit(request, "mcp_client.disconnected", application, application.name)
        messages.success(request, f"{application.name or 'The assistant'} was disconnected.")
    return redirect("mcp_settings")
