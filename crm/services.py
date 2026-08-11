import ipaddress

from .models import Activity, AuditLog


def client_ip(request):
    """Return the proxy-provided client address without trusting the left edge."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def audit(request, action, target=None, detail=""):
    AuditLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        action=action,
        target_type=target.__class__.__name__ if target else "",
        target_id=str(target.pk) if target else "",
        detail=(detail or "")[:500],
        ip_address=client_ip(request),
    )


def add_activity(*, request, subject, body="", contact=None, matter=None, kind=Activity.Kind.NOTE):
    return Activity.objects.create(
        contact=contact,
        matter=matter,
        kind=kind,
        subject=subject,
        body=body,
        created_by=request.user,
    )
