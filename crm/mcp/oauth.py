"""The MCP server's sign-in, built on django-oauth-toolkit.

Assistants register themselves (Client ID Metadata Documents or dynamic client
registration), then send the staff member to the consent page. The staff member
signs in as usual, including two-factor sign-in, and approves the assistant.
"""

from datetime import timedelta
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import Application
from oauth2_provider.views import AuthorizationView

from crm.middleware import CONTENT_SECURITY_POLICY

from .models import McpSettings

# Dynamic registration is open to anyone while MCP is on, so it is capped.
REGISTRATIONS_PER_HOUR = 30
MCP_PATH = "/mcp"


def resource_url(request):
    """The MCP server's canonical URL, which tokens must be issued for (RFC 8707)."""
    return request.build_absolute_uri(MCP_PATH)


def resource_metadata_url(request):
    return request.build_absolute_uri("/.well-known/oauth-protected-resource" + MCP_PATH)


class RegistrationPermission:
    """Dynamic client registration, while MCP is on, up to an hourly cap."""

    def has_permission(self, request):
        if not McpSettings.load().enabled:
            return False
        recent = Application.objects.filter(
            registration_source=Application.RegistrationSource.DCR,
            created__gte=timezone.now() - timedelta(hours=1),
        ).count()
        return recent < REGISTRATIONS_PER_HOUR


class MetadataDocumentPermission:
    """Client ID Metadata Documents, while MCP is on. The toolkit's fetcher accepts only
    https documents on public addresses."""

    def has_permission(self, request, client_id):
        return McpSettings.load().enabled


def origin_of(uri):
    parts = urlsplit(uri or "")
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


class ConsentView(AuthorizationView):
    """The toolkit's consent step, limited to active staff while MCP is on."""

    template_name = "mcp/consent.html"

    def dispatch(self, request, *args, **kwargs):
        if not McpSettings.load().enabled:
            return render(request, "mcp/off.html", status=403)
        user = request.user
        if user.is_authenticated and not (user.is_active and user.is_staff):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        redirect_uri = context.get("redirect_uri") or ""
        context["redirect_host"] = urlsplit(redirect_uri).hostname or ""
        context["settings_url"] = reverse("mcp_settings")
        return context

    def render_to_response(self, context, **kwargs):
        response = super().render_to_response(context, **kwargs)
        # Approving redirects the browser to the assistant, and the site-wide
        # ``form-action 'self'`` would block that. Allow only this client's callback.
        destination = origin_of(context.get("redirect_uri"))
        if destination:
            response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.replace(
                "form-action 'self'", f"form-action 'self' {destination}"
            )
        return response
