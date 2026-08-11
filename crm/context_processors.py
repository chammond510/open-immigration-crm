from django.db.utils import OperationalError, ProgrammingError

from .models import FirmProfile, TimeEntry


def firm_profile(request):
    try:
        profile = FirmProfile.objects.first()
    except (OperationalError, ProgrammingError):
        profile = None
    return {"firm_profile": profile}


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
