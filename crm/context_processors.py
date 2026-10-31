from django.db.utils import OperationalError, ProgrammingError

from .models import FirmProfile, TimeEntry


def firm_profile(request):
    try:
        profile = FirmProfile.objects.first()
    except (OperationalError, ProgrammingError):
        profile = None
    return {"firm_profile": profile}


# Which navigation link a page belongs to, by the start of its path.
NAV_SECTIONS = (
    ("/contacts/", "contacts"),
    ("/matters/", "matters"),
    ("/matter-parties/", "matters"),
    ("/checklist/", "matters"),
    ("/documents/", "matters"),
    ("/research/", "matters"),
    ("/shipments/", "matters"),
    ("/signatures/", "matters"),
    ("/translations/", "communications"),
    ("/work/", "work"),
    ("/time/", "work"),
    ("/notices/", "notices"),
    ("/communications/", "communications"),
    ("/billing/", "billing"),
    ("/intake/", "intake"),
    ("/reports/", "reports"),
    ("/connections/", "connections"),
    ("/events/", "connections"),
    ("/ai/", "assistant"),
    ("/settings/firm/", "firm"),
    ("/workflows/", "workflows"),
    ("/settings/custom-fields/", "custom_fields"),
    ("/imports/", "imports"),
    ("/settings/mcp/", "mcp"),
    ("/settings/ai/", "ai"),
    ("/settings/api/", "api"),
    ("/settings/team/", "team"),
    ("/accounts/", "account"),
    ("/notifications/", "notifications"),
)


def nav_section(request):
    if request.path == "/":
        return {"nav_section": "overview"}
    for prefix, section in NAV_SECTIONS:
        if request.path.startswith(prefix):
            return {"nav_section": section}
    return {"nav_section": ""}


def running_timer(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"running_timer": None}
    return {
        "running_timer": (
            TimeEntry.objects.select_related("matter", "contact")
            .filter(user=user, stopped_at__isnull=True)
            .first()
        )
    }
