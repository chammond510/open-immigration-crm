"""OpenID Connect sign-in: staff sign in with the firm's identity provider.

One connector for any OpenID Connect provider, with presets for Google Workspace and
Microsoft Entra ID. It uses the authorization code flow with the firm's own client ID
and secret, S256 PKCE, and a nonce. The provider's settings come from its discovery
document, whose issuer must be the one configured. The ID token comes straight from
the token endpoint over TLS, and its signature is still checked against the
provider's published keys, along with its issuer, audience, expiry, and nonce.

Who signed in is the issuer and subject. Microsoft's ``sub`` changes with the app
registration, so Entra uses the account's object ID (``oid``) within the firm's
tenant instead. An email address counts as verified only as each provider vouches
for it: Google's ``email_verified`` together with the Workspace domain (``hd``);
Entra's ``xms_edov`` optional claim, for a member of the firm's own tenant, never a
guest; and ``email_verified`` for other providers. The email must also be in one of
the firm's domains, on every sign-in.

The CRM's own second step is skipped only when the firm says its provider requires a
second factor and the ID token reports one (``amr`` including ``mfa``).

Google: https://developers.google.com/identity/openid-connect/openid-connect
Microsoft: https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
OpenID Connect Core: https://openid.net/specs/openid-connect-core-1_0.html
"""

import base64
import json
import re
import secrets
import time
from urllib.parse import quote, urlencode

from jwcrypto import jwk, jwt
from jwcrypto.common import JWException

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
    StaffIdentity,
)
from crm.connect.http import ProviderClient, check_url
from crm.connect.registry import register

GOOGLE = "https://accounts.google.com"
MICROSOFT = "https://login.microsoftonline.com/{tenant}/v2.0"
TENANT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SCOPES = "openid email profile"
ALGORITHMS = ["RS256", "ES256"]
# How far in the future an ID token's issue time may be, for clocks that differ.
CLOCK_SKEW_SECONDS = 120
UNVERIFIED = "That account could not be checked. Sign in again."


def domains(value):
    """The firm's email domains, in the order given."""
    found = (part.strip().lower().lstrip("@") for part in str(value).split(","))
    return list(dict.fromkeys(part for part in found if part))


def is_true(value):
    """Providers send booleans, and some send the string "true"."""
    return value is True or str(value).lower() == "true"


@register
class OidcConnector(Connector):
    key = "oidc"
    name = "OpenID Connect (Google, Microsoft Entra ID, and others)"
    description = (
        "Staff sign in with the firm's identity provider, such as Google Workspace or "
        "Microsoft Entra ID. Only existing staff accounts can sign in this way."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SIGN_IN})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "client_id",
                "Client ID",
                secret=False,
                help_text="From the app the firm registered at its identity provider.",
            ),
            CredentialField(
                "client_secret",
                "Client secret",
                help_text="From the same app. Note when it expires, and connect again with a "
                "new one before then.",
            ),
        ),
        label="The firm's sign-in app",
    )
    settings_fields = (
        SettingField(
            "provider",
            "Identity provider",
            choices=(
                ("google", "Google Workspace"),
                ("microsoft", "Microsoft Entra ID"),
                ("other", "Another OpenID Connect provider, such as Okta or JumpCloud"),
            ),
            default="google",
        ),
        SettingField(
            "domains",
            "Staff email domains",
            help_text="Such as firm.example.test; separate several with commas. Only "
            "accounts with an email address in these domains can sign in. For Google "
            "Workspace, the first is the Workspace domain.",
        ),
        SettingField(
            "tenant",
            "Microsoft tenant ID",
            required=False,
            help_text="For Microsoft Entra ID: the Directory (tenant) ID, a long code with "
            "dashes, from the app's overview page.",
        ),
        SettingField(
            "issuer",
            "Issuer address",
            required=False,
            help_text="For another provider: its issuer, such as "
            "https://firm.okta.example.test, exactly as its discovery document gives it.",
        ),
        SettingField(
            "two_factor",
            "The provider's second factor",
            choices=(
                ("crm", "Staff also use the CRM's own two-factor sign-in, if they have it"),
                (
                    "provider",
                    "Skip the CRM's second step when the provider reports a second factor",
                ),
            ),
            default="crm",
            help_text="The second choice works only if the provider reports it in the ID "
            "token (the amr claim); otherwise the CRM's own step still applies.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "When staff sign in: the firm's client ID and secret, and the one-time code "
            "the identity provider gave",
        ),
        receives=(
            "For the person signing in: their ID at the identity provider, name, email "
            "address and whether the provider vouches for it, the organization their "
            "account belongs to, and whether they used a second factor",
        ),
    )

    # Configuration

    @property
    def provider(self):
        return self.setting("provider")

    def issuer(self):
        if self.provider == "google":
            return GOOGLE
        if self.provider == "microsoft":
            tenant = str(self.setting("tenant")).strip().lower()
            if not TENANT_ID.fullmatch(tenant):
                raise ConnectorError("Enter the Microsoft tenant ID: a long code with dashes.")
            return MICROSOFT.format(tenant=tenant)
        issuer = str(self.setting("issuer")).strip()
        if not issuer:
            raise ConnectorError("Enter the identity provider's issuer address.")
        check_url(issuer)
        return issuer

    def accepted_issuers(self):
        issuer = self.issuer()
        # Google's ID tokens name their issuer either way.
        return {issuer, "accounts.google.com"} if self.provider == "google" else {issuer}

    def client(self):
        return ProviderClient(
            name="The identity provider", headers={"Accept": "application/json"}, retries=1
        )

    def discovery(self):
        issuer = self.issuer()
        found = self.client().get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if not isinstance(found, dict) or found.get("issuer") != issuer:
            raise ConnectorError(
                "The identity provider's settings name a different issuer. Check the "
                "issuer address or tenant ID."
            )
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            check_url(str(found.get(name) or ""))
        return found

    def client_id(self):
        client_id = str(self.credentials.get("client_id", "")).strip()
        if not client_id or not self.credentials.get("client_secret"):
            raise ConnectorError("Enter the client ID and secret from the firm's app.")
        return client_id

    def test_connection(self):
        client_id = self.client_id()
        if not domains(self.setting("domains")):
            raise ConnectorError("Enter the staff email domains.")
        found = self.discovery()
        # A made-up code shows whether the provider accepts the client ID and secret:
        # it refuses the client with invalid_client, or else only the code.
        response = self.token_request(
            found,
            client_id,
            {"grant_type": "authorization_code", "code": secrets.token_urlsafe(16)},
        )
        if response.status_code == 401 or error_of(response) == "invalid_client":
            raise ConnectorError(
                "The identity provider did not accept the client ID and secret.", status=401
            )

    # Signing in

    def sign_in_url(self, redirect_uri, state, nonce, code_challenge):
        endpoint = self.discovery()["authorization_endpoint"]
        query = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if self.provider == "google":
            # Only a hint for Google's account chooser; the hd claim is what is checked.
            query.update(hd=domains(self.setting("domains"))[0], prompt="select_account")
        elif self.provider == "microsoft":
            query["prompt"] = "select_account"
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(query)}"

    def token_request(self, found, client_id, data):
        secret = self.credentials.get("client_secret", "")
        methods = found.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        kwargs = {"data": {**data, "client_id": client_id}}
        if "client_secret_post" in methods:
            kwargs["data"]["client_secret"] = secret
        else:
            pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
            kwargs["headers"] = {"Authorization": "Basic " + base64.b64encode(pair).decode()}
        return self.client().request(
            "POST", found["token_endpoint"], expected=(200, 400, 401), **kwargs
        )

    def verify_sign_in(self, params, redirect_uri, code_verifier, nonce):
        issuers = self.accepted_issuers()
        if params.get("iss") and params["iss"] not in issuers:
            raise ConnectorError("The answer came from a different identity provider.")
        found = self.discovery()
        client_id = self.client_id()
        response = self.token_request(
            found,
            client_id,
            {
                "grant_type": "authorization_code",
                "code": str(params.get("code", "")),
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        if response.status_code != 200:
            if response.status_code == 401 or error_of(response) == "invalid_client":
                raise ConnectorError("The identity provider no longer accepts the firm's app.")
            raise ConnectorError("The identity provider refused the sign-in. Try again.")
        try:
            id_token = str(response.json().get("id_token") or "")
        except (ValueError, AttributeError):
            raise ConnectorError("The identity provider's answer was not understood.") from None
        claims = self.verified_claims(found, id_token, client_id, nonce, issuers)
        return self.identity(claims)

    def verified_claims(self, found, id_token, client_id, nonce, issuers):
        keys = self.client().get(found["jwks_uri"])
        try:
            token = jwt.JWT(
                jwt=id_token,
                key=jwk.JWKSet.from_json(json.dumps(keys)),
                algs=ALGORITHMS,
                check_claims={"exp": None},
            )
            claims = json.loads(token.claims)
        except (JWException, ValueError, TypeError):
            raise ConnectorError(UNVERIFIED) from None
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        issued = claims.get("iat")
        if (
            claims.get("iss") not in issuers
            or audiences != [client_id]
            or claims.get("azp", client_id) != client_id
            or not isinstance(issued, int | float)
            or issued > time.time() + CLOCK_SKEW_SECONDS
            or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce or "-")
        ):
            raise ConnectorError(UNVERIFIED)
        return claims

    def identity(self, claims):
        email = str(claims.get("email") or "").strip().lower()
        subject, verified = str(claims.get("sub") or ""), is_true(claims.get("email_verified"))
        allowed = domains(self.setting("domains"))
        if self.provider == "google":
            if str(claims.get("hd") or "").lower() != allowed[0]:
                raise ConnectorError("Sign in with the firm's Google Workspace account.")
        elif self.provider == "microsoft":
            tenant = str(self.setting("tenant")).strip().lower()
            if str(claims.get("tid") or "").lower() != tenant or str(claims.get("acct", 0)) != "0":
                raise ConnectorError("Sign in with an account that belongs to the firm.")
            subject = str(claims.get("oid") or "")
            verified = is_true(claims.get("xms_edov"))
        if not subject:
            raise ConnectorError(UNVERIFIED)
        if email.rpartition("@")[2] not in allowed:
            raise ConnectorError("That account's email address is not in the firm's domains.")
        amr = claims.get("amr") if isinstance(claims.get("amr"), list) else []
        return StaffIdentity(
            issuer=str(claims["iss"]) if self.provider != "google" else GOOGLE,
            subject=subject,
            email=email,
            email_verified=verified,
            name=str(claims.get("name") or "")[:200],
            two_factor=self.setting("two_factor") == "provider" and "mfa" in amr,
        )


def error_of(response):
    try:
        return str(response.json().get("error") or "")
    except (ValueError, AttributeError):
        return ""
