"""Staff sign-in through the firm's identity provider.

Each active, firm-wide connection with the Sign-in capability, once a superuser
has reviewed what it sends and receives, puts a button on the sign-in page. The
flow is OAuth's authorization code flow, with a one-time state, nonce, and S256
PKCE verifier kept in the session for ten minutes. The connector checks the
provider's answer and returns a ``StaffIdentity``.

Only existing, active staff accounts can sign in this way, and none is ever
created: an identity already linked to an account, or, the first time, one whose
verified email matches exactly one account's email. After that, the link holds
by the provider's own issuer and subject, so a changed email does not move it.

The CRM's own two-factor step still follows, as after a password, unless the
connector reports that the provider always requires a second factor. Signing in
with a username and password keeps working, so a superuser can always get in.
"""

import secrets
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

from crm.models import AuditLog
from crm.services import client_ip
from crm.two_factor import SESSION_PENDING_KEY, SESSION_VERIFIED_KEY, confirmed_device

from .base import Capability, ConnectorError
from .models import Connection, SignInLink
from .oauth import pkce_pair

PENDING_KEY = "staff_sign_in_pending"
STATE_TTL_SECONDS = 600
BACKEND = "django.contrib.auth.backends.ModelBackend"


def sign_in_connections():
    """The connections that put a button on the sign-in page, oldest first."""
    return [
        connection
        for connection in Connection.objects.filter(
            status=Connection.Status.ACTIVE, owner__isnull=True
        ).order_by("created_at")
        if connection.connector_class is not None
        and Capability.SIGN_IN in connection.enabled_capabilities
        and connection.data_flows_acknowledged
    ]


def callback_url(request):
    """The exact return address to register at the identity provider."""
    return request.build_absolute_uri(reverse("staff_sign_in_callback"))


def record(request, action, user, connection, detail=""):
    AuditLog.objects.create(
        user=user,
        action=action,
        target_type="Connection",
        target_id=str(connection.pk),
        detail=detail[:500],
        ip_address=client_ip(request),
    )


@require_GET
def start(request, pk):
    """Remember a one-time state, nonce, and verifier, and go to the provider."""
    connection = next((each for each in sign_in_connections() if each.pk == pk), None)
    if connection is None:
        raise Http404
    target = request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = ""
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    try:
        url = connection.connector().sign_in_url(callback_url(request), state, nonce, challenge)
    except ConnectorError as exc:
        messages.error(request, f"Signing in with {connection.label} is not working: {exc}")
        return redirect("login")
    request.session[PENDING_KEY] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "connection": str(connection.pk),
        "next": target,
        "created": time.time(),
    }
    return redirect(url)


@require_GET
def callback(request):
    """Check the provider's answer, find the staff account, and sign in."""
    pending = request.session.pop(PENDING_KEY, None) or {}
    given = request.GET.get("state", "")
    if (
        not pending
        or not given
        or not secrets.compare_digest(given, pending["state"])
        or time.time() - pending["created"] > STATE_TTL_SECONDS
    ):
        messages.error(request, "That sign-in expired or did not start here. Try again.")
        return redirect("login")
    connection = next(
        (each for each in sign_in_connections() if str(each.pk) == pending["connection"]), None
    )
    if connection is None:
        messages.error(request, "That way of signing in was turned off. Use your password.")
        return redirect("login")
    if request.GET.get("error") or not request.GET.get("code"):
        messages.error(request, f"{connection.label} did not sign you in.")
        return redirect("login")
    try:
        identity = connection.connector().verify_sign_in(
            request.GET.dict(), callback_url(request), pending["verifier"], pending["nonce"]
        )
    except ConnectorError as exc:
        record(request, "user.sign_in_failed", None, connection, str(exc))
        messages.error(request, f"{connection.label} sign-in could not be checked: {exc}")
        return redirect("login")
    user = staff_account(request, connection, identity)
    if user is None:
        record(request, "user.sign_in_refused", None, connection, "No matching staff account")
        messages.error(
            request,
            f"No staff account matches that {connection.label} sign-in. Ask a superuser to "
            "check that your account has the same email address, or sign in with your "
            "username and password.",
        )
        return redirect("login")
    return finish(request, user, connection, identity, pending["next"])


def staff_account(request, connection, identity):
    """The active staff account this identity signs in to, linking it the first time."""
    now = timezone.now()
    link = (
        SignInLink.objects.select_related("user")
        .filter(issuer=identity.issuer, subject=identity.subject)
        .first()
    )
    if link is not None:
        if not (link.user.is_active and link.user.is_staff):
            return None
        link.connection, link.last_used_at = connection, now
        link.save(update_fields=["connection", "last_used_at"])
        return link.user
    if not (identity.email and identity.email_verified):
        return None
    matches = list(
        get_user_model().objects.filter(
            email__iexact=identity.email, is_active=True, is_staff=True
        )[:2]
    )
    if len(matches) != 1:
        return None
    link, _ = SignInLink.objects.get_or_create(
        issuer=identity.issuer,
        subject=identity.subject,
        defaults={"user": matches[0], "connection": connection, "last_used_at": now},
    )
    record(request, "user.sign_in_linked", link.user, connection)
    return link.user


def finish(request, user, connection, identity, target):
    """Sign in, or hold at the CRM's own second step as a password sign-in would."""
    if confirmed_device(user) is not None and not identity.two_factor:
        request.session.cycle_key()
        request.session[SESSION_PENDING_KEY] = {
            "user_id": user.pk,
            "backend": BACKEND,
            "next": target,
            "started_at": timezone.now().timestamp(),
        }
        record(request, "user.sign_in_started", user, connection)
        return redirect("two_factor_verify")
    login(request, user, backend=BACKEND)
    if identity.two_factor:
        request.session[SESSION_VERIFIED_KEY] = True
    record(
        request,
        "user.signed_in",
        user,
        connection,
        "Provider's second factor" if identity.two_factor else "",
    )
    return redirect(target or settings.LOGIN_REDIRECT_URL)
