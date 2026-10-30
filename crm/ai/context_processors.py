from django.db.utils import OperationalError, ProgrammingError

from .models import AISettings


def ai_features(request):
    """Whether the assistant is on, for the sidebar."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"ai_assistant_on": False}
    try:
        return {"ai_assistant_on": AISettings.load().feature_on("matter_assistant")}
    except (OperationalError, ProgrammingError):
        return {"ai_assistant_on": False}
