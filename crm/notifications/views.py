from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from crm.roles import is_staff

from .models import Notification

staff_required = user_passes_test(is_staff, login_url="login")
SHOWN = 50


@staff_required
@require_GET
def notification_list(request):
    notifications = request.user.notifications.select_related("actor")[:SHOWN]
    return render(request, "notifications/notification_list.html", {"notifications": notifications})


@staff_required
@require_GET
def notification_open(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    # Links are made by the CRM from its own URLs; anything else goes to the list.
    if notification.url.startswith("/") and not notification.url.startswith("//"):
        return redirect(notification.url)
    return redirect("notification_list")


@staff_required
@require_POST
def notifications_read(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notification_list")


def unread_notifications(request):
    """The signed-in person's unread count, for the bell in the top bar."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"unread_notifications": 0}
    return {"unread_notifications": user.notifications.filter(read_at__isnull=True).count()}
