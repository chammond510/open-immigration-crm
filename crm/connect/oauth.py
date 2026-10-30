"""OAuth 2.0 authorization-code sign-in with PKCE, using the firm's own app registration.

Sign-in starts from a link rather than a form: the content security policy's
``form-action 'self'`` would block a form submission that redirects to the
provider.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from .base import ConnectorError, OAuthAuth
from .models import Connection, ConnectorApp

PENDING_KEY = "connect_oauth_pending"
STATE_TTL_SECONDS = 600
TOKEN_RESPONSE_MAX_BYTES = 256 * 1024


def callback_url(request):
    """The exact redirect URI staff register in the provider's developer console."""
    return request.build_absolute_uri(reverse("connection_oauth_callback"))


def requested_scopes(cls, capabilities):
    scopes = set()
    for capability in capabilities:
        scopes.update(cls.auth.scopes.get(capability, ()))
    return sorted(scopes)


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def authorization_url(request, connection):
    """Remember a one-time state and PKCE verifier, and return the provider URL to visit."""
    cls = connection.connector_class
    if cls is None or not isinstance(cls.auth, OAuthAuth):
        raise ValidationError("This connection does not sign in with OAuth.")
    app = ConnectorApp.objects.filter(connector_key=cls.key).first()
    if app is None:
        raise ValidationError(f"A superuser must first enter the firm's {cls.name} app details.")
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    request.session[PENDING_KEY] = {
        "state": state,
        "connection": str(connection.pk),
        "verifier": verifier,
        "created": time.time(),
    }
    query = {
        "response_type": "code",
        "client_id": app.client_id,
        "redirect_uri": callback_url(request),
        "scope": " ".join(requested_scopes(cls, connection.enabled_capabilities)),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **cls.auth.extra_authorize_params,
    }
    separator = "&" if "?" in cls.auth.authorize_url else "?"
    return f"{cls.auth.authorize_url}{separator}{urlencode(query)}"


def post_form(url, data):
    """POST a form to a provider over HTTPS and return its JSON answer."""
    if not url.startswith("https://"):
        raise ConnectorError("The provider's token address must use HTTPS.")
    request = urllib.request.Request(
        url,
        data=urlencode(data).encode(),
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(TOKEN_RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ConnectorError(f"The provider refused the sign-in (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectorError("The provider could not be reached to finish sign-in.") from exc
    if len(body) > TOKEN_RESPONSE_MAX_BYTES:
        raise ConnectorError("The provider's sign-in response was too large.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ConnectorError("The provider's sign-in response was not understood.") from exc


def finish(request):
    """Validate the callback, exchange the code, and store the tokens. Returns the connection."""
    pending = request.session.pop(PENDING_KEY, None) or {}
    state = request.GET.get("state", "")
    if (
        not pending
        or not secrets.compare_digest(state, pending.get("state", ""))
        or time.time() - pending.get("created", 0) > STATE_TTL_SECONDS
    ):
        raise ValidationError("This sign-in link expired or did not come from this CRM.")
    connection = Connection.objects.filter(pk=pending["connection"]).first()
    cls = connection.connector_class if connection else None
    if cls is None or not isinstance(cls.auth, OAuthAuth):
        raise ValidationError("The connection for this sign-in no longer exists.")
    if request.GET.get("error") or not request.GET.get("code"):
        raise ConnectorError("Sign-in was cancelled or refused at the provider.")
    app = ConnectorApp.objects.get(connector_key=cls.key)
    tokens = post_form(
        cls.auth.token_url,
        {
            "grant_type": "authorization_code",
            "code": request.GET["code"],
            "redirect_uri": callback_url(request),
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "code_verifier": pending["verifier"],
        },
    )
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise ConnectorError("The provider did not return an access token.")
    expires_in = tokens.get("expires_in")
    connection.credentials = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_at": time.time() + int(expires_in) if expires_in else None,
    }
    connection.granted_scopes = sorted(str(tokens.get("scope", "")).split())
    connection.status = Connection.Status.ACTIVE
    connection.last_error = ""
    connection.last_success_at = timezone.now()
    connection.save()
    return connection
