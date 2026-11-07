import ipaddress

from django.conf import settings

from .models import Activity, AuditLog


def client_ip(request):
    """Walk a forwarded chain only through explicitly trusted proxy addresses."""
    try:
        peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
    except (ValueError, TypeError):
        return None
    networks = settings.TRUSTED_PROXY_NETWORKS
    if not any(peer in network for network in networks):
        return str(peer)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    for value in reversed(forwarded.split(",")):
        try:
            address = ipaddress.ip_address(value.strip())
        except ValueError:
            # A malformed chain is not evidence of a different client address.
            return str(peer)
        if not any(address in network for network in networks):
            return str(address)
    return str(peer)


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
