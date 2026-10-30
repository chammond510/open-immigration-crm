"""OAuth 2.0 authorization-code sign-in with PKCE, using the firm's own app registration.

Sign-in starts from a link rather than a form: the content security policy's
``form-action 'self'`` would block a form submission that redirects to the
provider.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .base import ConnectorError, OAuthAuth
from .http import ProviderClient
from .models import Connection, ConnectorApp

PENDING_KEY = "connect_oauth_pending"
STATE_TTL_SECONDS = 600
TOKEN_RESPONSE_MAX_BYTES = 256 * 1024
# Refresh this long before the access token expires.
REFRESH_MARGIN_SECONDS = 300


class TokenRefused(ConnectorError):
    """The provider refused a code or refresh token (HTTP 400 or 401)."""


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
    """POST a form to a provider's token endpoint over HTTPS and return its JSON answer."""
    client = ProviderClient(
        name="The provider",
        headers={"Accept": "application/json"},
        retries=1,
        max_bytes=TOKEN_RESPONSE_MAX_BYTES,
    )
    try:
        tokens = client.post(url, data=data)
    except ConnectorError as exc:
        if exc.status in (400, 401):
            raise TokenRefused("The provider refused the sign-in.", status=exc.status) from None
        raise
    if not isinstance(tokens, dict):
        raise ConnectorError("The provider's sign-in response was not understood.")
    return tokens


def stored_tokens(tokens, previous=None):
    """Credentials to keep from a token response; a refresh may omit the refresh token."""
    previous = previous or {}
    expires_in = tokens.get("expires_in")
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_at": time.time() + int(expires_in) if expires_in else None,
    }


def needs_refresh(credentials):
    expires_at = (credentials or {}).get("expires_at")
    return bool(expires_at) and expires_at - time.time() < REFRESH_MARGIN_SECONDS


def mark_needs_sign_in(connection, message):
    connection.status = Connection.Status.NEEDS_REAUTH
    connection.last_error = message
    connection.last_error_at = timezone.now()
    connection.save(update_fields=["status", "last_error", "last_error_at", "updated_at"])


def access_token(connection):
    """A current access token for an OAuth connection, refreshed first if it is about to expire.

    The refresh runs under a row lock, so two workers never spend the same refresh
    token. Providers that rotate refresh tokens get the new one saved once. A refused
    refresh marks the connection as needing sign-in again.
    """
    if not needs_refresh(connection.credentials):
        return (connection.credentials or {}).get("access_token", "")
    failure = ""
    with transaction.atomic():
        locked = Connection.objects.select_for_update().get(pk=connection.pk)
        credentials = locked.credentials or {}
        if needs_refresh(credentials):
            cls = locked.connector_class
            app = ConnectorApp.objects.filter(connector_key=locked.connector_key).first()
            if not credentials.get("refresh_token") or app is None or cls is None:
                failure = "Sign in again to keep this connection working."
            else:
                try:
                    tokens = post_form(
                        cls.auth.token_url,
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": credentials["refresh_token"],
                            "client_id": app.client_id,
                            "client_secret": app.client_secret,
                        },
                    )
                except TokenRefused:
                    failure = "The provider ended this sign-in. Sign in again to reconnect."
                else:
                    if not tokens.get("access_token"):
                        raise ConnectorError("The provider did not return an access token.")
                    credentials = stored_tokens(tokens, credentials)
                    locked.credentials = credentials
                    locked.save(update_fields=["credentials", "updated_at"])
    if failure:
        # Outside the transaction, so raising does not undo the status change.
        mark_needs_sign_in(Connection.objects.get(pk=connection.pk), failure)
        connection.status = Connection.Status.NEEDS_REAUTH
        raise ConnectorError(failure)
    connection.credentials = credentials
    return credentials["access_token"]


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
    if not tokens.get("access_token"):
        raise ConnectorError("The provider did not return an access token.")
    connection.credentials = stored_tokens(tokens)
    connection.granted_scopes = sorted(str(tokens.get("scope", "")).split())
    connection.status = Connection.Status.ACTIVE
    connection.last_error = ""
    connection.last_success_at = timezone.now()
    connection.save()
    return connection
