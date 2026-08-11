from django.db.utils import OperationalError, ProgrammingError

from .models import FirmProfile


def firm_profile(request):
    try:
        profile = FirmProfile.objects.first()
    except (OperationalError, ProgrammingError):
        profile = None
    return {"firm_profile": profile}
