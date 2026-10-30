from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from crm.connect.base import Capability
from crm.connect.models import Connection
from crm.connect.services import record_audit

from .catalog import EVENT_TYPES
from .forms import EventSettingsForm

superuser_required = user_passes_test(
    lambda user: user.is_active and user.is_superuser, login_url="login"
)


@superuser_required
@require_POST
def connection_events(request, pk):
    connection = get_object_or_404(
        Connection.objects.exclude(status=Connection.Status.DISCONNECTED), pk=pk
    )
    if Capability.AUTOMATION not in connection.enabled_capabilities:
        messages.error(request, "This connection does not send automation events.")
        return redirect("connection_detail", pk=pk)
    form = EventSettingsForm(request.POST)
    if form.is_valid():
        events = [name for name in EVENT_TYPES if name in form.cleaned_data["events"]]
        connection.config = {
            **(connection.config or {}),
            "events": events,
            "detail": form.cleaned_data["detail"],
            # Links in events point here; deliveries are built outside any request.
            "site_url": request.build_absolute_uri("/").rstrip("/"),
        }
        connection.save(update_fields=["config", "updated_at"])
        record_audit(
            request.user,
            "connection.events_changed",
            connection,
            f"{len(events)} event type{'s' if len(events) != 1 else ''}, "
            f"{form.cleaned_data['detail']} detail",
        )
        messages.success(
            request,
            f"Saved. {len(events)} event type{'s' if len(events) != 1 else ''} selected."
            if events
            else "Saved. No events are selected, so nothing will be sent.",
        )
    else:
        messages.error(request, "Choose what each event includes.")
    return redirect("connection_detail", pk=pk)
