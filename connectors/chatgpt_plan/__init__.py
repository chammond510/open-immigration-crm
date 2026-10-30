"""ChatGPT plan: a staff member's own ChatGPT plan powers their AI requests (Beta, behind a flag).

Sign in with ChatGPT lets open-source apps use a signed-in user's plan. Each staff
member connects their own plan, which runs only their own requests, never another
person's or background work. The connector is offered only while CONNECTOR_FLAGS
lists ``chatgpt_plan``. It stays Beta until OpenAI confirms in writing that a
firm-hosted web app used by several staff may use plan usage this way, and its data
terms are known.

Sign-in: OpenAI accepts only a loopback callback on the user's own computer. After
signing in, the browser lands on an address that cannot load, and the staff member
pastes it back. The first sign-in registers the CRM (``dynamic_agent_client``), and
the client ID OpenAI issues for that user and workspace is kept for later sign-ins.
The ID token is checked against OpenAI's published keys, with its issuer, audience,
expiry, and nonce, and the grant must include plan usage. Access tokens last an
hour. Refresh tokens rotate on every use, so core refreshes under a row lock.
Disconnecting revokes the refresh token.

Requests: the Responses API with ``store: false`` and ``stream: true``, read until
``response.completed``. Function tools go in a namespace, and fields this flow
refuses, such as ``max_output_tokens``, are left out. When the plan's limit is
reached the request stops; OpenAI never bills another way.

Reference: https://developers.openai.com/siwc/token-sharing-open-source
"""

import json
import time

from jwcrypto import jwk, jwt
from jwcrypto.common import JWException

from connectors.openai import read_response, text_format, to_items
from crm.connect import oauth
from crm.connect.base import (
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    DataFlows,
    KeyExchangeAuth,
    Maturity,
    SettingField,
)
from crm.connect.http import ProviderClient
from crm.connect.registry import register

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
REVOKE_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
ISSUER = "https://auth.openai.com"
API_URL = "https://api.openai.com/v1"
RESOURCE = API_URL
LOOPBACK = "http://127.0.0.1:1455/auth/callback"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
DYNAMIC_CLIENT = "dynamic_agent_client"
APP_NAME = "Open Immigration CRM"
USAGE_URL = "https://chatgpt.com/settings/usage"
NAMESPACE = "crm"
# Refresh errors that mean the sign-in is over and must be done again.
ENDED = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}
PLAN_ERRORS = {
    "subscription_sharing_user_not_eligible": "This ChatGPT account or workspace cannot use "
    "its plan in other apps. Use another AI connection.",
    "subscription_sharing_usage_limit_exceeded": "Your ChatGPT plan's usage limit for this app "
    f"has been reached. Manage usage at {USAGE_URL}, or use another AI connection.",
    "subscription_sharing_usage_unavailable": "ChatGPT could not check your plan's usage. Try "
    "again in a few minutes.",
    "subscription_sharing_user_unavailable": "ChatGPT could not check your account. Try again "
    "in a few minutes.",
    "subscription_sharing_invalid_user": "ChatGPT could not confirm this sign-in. Sign in again.",
    "subscription_sharing_unsupported_capability": "ChatGPT plan usage does not support part of "
    "this request.",
}


def host_id(connection):
    """The stable, opaque host ID OpenAI asks for: this connection's random UUID."""
    return f"urn:uuid:{connection.pk}"


def error_code(response):
    try:
        body = response.json()
    except ValueError:
        return "", ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or ""), str(error.get("message") or "")
    if isinstance(error, str):
        return error, str(body.get("error_description") or "")
    return "", str(body.get("detail") or "") if isinstance(body, dict) else ""


def token_request(data):
    """POST to OpenAI's token endpoint; return its answer or raise with a clear message."""
    client = ProviderClient(name="ChatGPT", headers={"Accept": "application/json"}, retries=1)
    response = client.request("POST", TOKEN_URL, data=data, expected=(200, 400, 401))
    if response.status_code == 200:
        try:
            return response.json()
        except ValueError:
            raise ConnectorError("ChatGPT's sign-in answer was not understood.") from None
    code, _ = error_code(response)
    if code in ENDED:
        raise ConnectorError("ChatGPT ended this sign-in. Sign in again.", status=401)
    if code == "invalid_client":
        raise ConnectorError(
            "ChatGPT no longer recognizes this connection. Disconnect it and connect again.",
            status=401,
        )
    raise ConnectorError("ChatGPT refused the sign-in.", status=response.status_code)


def verified_claims(id_token, client_id, nonce):
    """The ID token's claims, after checking its signature, issuer, audience, expiry, and nonce."""
    keys = ProviderClient(name="ChatGPT", retries=1).get(JWKS_URL)
    try:
        token = jwt.JWT(
            jwt=str(id_token or ""),
            key=jwk.JWKSet.from_json(json.dumps(keys)),
            algs=["RS256", "ES256"],
            check_claims={"iss": ISSUER, "aud": client_id, "exp": None},
        )
        claims = json.loads(token.claims)
    except (JWException, ValueError, TypeError):
        raise ConnectorError("ChatGPT's sign-in could not be verified. Sign in again.") from None
    if not nonce or claims.get("nonce") != nonce:
        raise ConnectorError("ChatGPT's sign-in could not be verified. Sign in again.")
    return claims


def stored(tokens, previous):
    """Credentials to keep, from a token answer and the earlier credentials."""
    if not tokens.get("access_token"):
        raise ConnectorError("ChatGPT did not return an access token.")
    scopes = str(tokens.get("scope") or "").split() or previous.get("scopes") or []
    return {
        **previous,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token", ""),
        "id_token": tokens.get("id_token") or previous.get("id_token", ""),
        "expires_at": time.time() + int(tokens.get("expires_in") or 3600),
        "scopes": scopes,
    }


def read_stream(text):
    """The final response from a Responses API event stream, or why there is none."""
    for block in text.split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "response.completed":
            return event.get("response") or {}
        if kind in ("response.failed", "error"):
            error = (event.get("response") or {}).get("error") or event.get("error") or event
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            raise ConnectorError(
                PLAN_ERRORS.get(code) or f"ChatGPT could not answer ({code or 'no reason given'})."
            )
        if kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            raise ConnectorError(
                f"ChatGPT stopped before finishing ({details.get('reason') or 'no reason given'})."
            )
    raise ConnectorError("ChatGPT's answer was cut off. Try again.")


def model_options(connector):
    return connector.models()


@register
class ChatGPTPlanConnector(Connector):
    key = "chatgpt_plan"
    name = "ChatGPT plan"
    description = (
        "Each staff member can power their own AI requests with their ChatGPT plan, by "
        "signing in with ChatGPT. Beta: offered only behind a flag."
    )
    maturity = Maturity.BETA
    flag = "chatgpt_plan"
    capabilities = frozenset({Capability.AI_MODELS})
    connection_scope = ConnectionScope.USER
    auth = KeyExchangeAuth(
        authorize_url=AUTHORIZE_URL,
        docs_url="https://developers.openai.com/siwc/token-sharing-open-source",
        loopback_redirect=LOOPBACK,
        button_label="Continue with ChatGPT",
    )
    settings_fields = (
        SettingField(
            "model",
            "Model",
            required=False,
            options=model_options,
            help_text="From the models your plan offers. Left blank, the first one is used.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "What each AI feature you use sends, such as the contact and matter details it "
            "works from, to your own ChatGPT plan, with storing turned off",
        ),
        receives=("The model's answers",),
    )
    outbound_actions = {"generate": Capability.AI_MODELS}
    usage_label = "Using ChatGPT plan"
    usage_url = USAGE_URL

    def authorize_query(self, callback_url, code_challenge, state, nonce=""):
        client_id = self.credentials.get("client_id") or DYNAMIC_CLIENT
        query = {
            "client_id": client_id,
            "ext_agent_host_id": host_id(self.connection),
            "response_type": "code",
            "redirect_uri": callback_url,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if client_id == DYNAMIC_CLIENT:
            query["agent_name_hint"] = APP_NAME
        elif self.credentials.get("id_token"):
            query["id_token_hint"] = self.credentials["id_token"]
        return query

    def exchange_code(self, code, code_verifier, callback_url, *, returned=None, nonce=""):
        returned = returned or {}
        saved = self.credentials.get("client_id", "")
        issued = returned.get("client_id", "")
        if saved and issued and issued != saved:
            raise ConnectorError("This sign-in came back for a different ChatGPT registration.")
        client_id = saved or issued
        if not client_id or client_id == DYNAMIC_CLIENT:
            raise ConnectorError("ChatGPT did not return the CRM's registration. Sign in again.")
        tokens = token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": callback_url,
                "resource": RESOURCE,
            }
        )
        claims = verified_claims(tokens.get("id_token"), client_id, nonce)
        credentials = stored(tokens, {"client_id": client_id})
        if PLAN_SCOPE not in credentials["scopes"]:
            raise ConnectorError(
                "ChatGPT plan usage was not allowed at sign-in. Sign in again and allow it."
            )
        credentials.update(subject=str(claims.get("sub") or ""), email=claims.get("email", ""))
        return credentials

    def refresh_credentials(self, credentials):
        if not credentials.get("refresh_token") or not credentials.get("client_id"):
            raise ConnectorError("Sign in again to keep this connection working.", status=401)
        tokens = token_request(
            {
                "grant_type": "refresh_token",
                "client_id": credentials["client_id"],
                "refresh_token": credentials["refresh_token"],
                "resource": RESOURCE,
            }
        )
        return stored(tokens, credentials)

    def revoke(self):
        if not self.credentials.get("refresh_token"):
            return
        ProviderClient(name="ChatGPT", retries=2).request(
            "POST",
            REVOKE_URL,
            data={
                "token": self.credentials["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": self.credentials.get("client_id", ""),
            },
        )

    def client(self):
        token = oauth.access_token(self.connection)
        return ProviderClient(
            API_URL,
            name="ChatGPT",
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
            retries=1,
        )

    def call(self, path, **kwargs):
        response = self.client().request(
            "POST" if "json" in kwargs else "GET",
            path,
            expected=(200, 400, 401, 403, 429, 503),
            **kwargs,
        )
        if response.status_code == 200:
            return response
        code, detail = error_code(response)
        if code in PLAN_ERRORS:
            raise ConnectorError(PLAN_ERRORS[code], status=response.status_code)
        if response.status_code == 401:
            raise ConnectorError("ChatGPT did not accept this sign-in. Sign in again.", status=401)
        raise ConnectorError(
            f"ChatGPT refused the request ({code or detail[:120] or response.status_code}).",
            status=response.status_code,
        )

    def models(self):
        """The models this plan offers, as (slug, name) choices."""
        try:
            listed = self.call("/models").json()
        except ValueError:
            raise ConnectorError("ChatGPT's model list was not understood.") from None
        models = listed.get("models") or listed.get("data") or []
        return [
            (str(model["slug"]), str(model.get("display_name") or model["slug"]))
            for model in models
            if isinstance(model, dict) and model.get("slug") and model.get("visibility") == "list"
        ]

    def test_connection(self):
        if not self.models():
            raise ConnectorError("This ChatGPT plan offers no models to other apps.")

    def perform(self, action, payload):
        if action != "generate":
            raise ConnectorError(f"ChatGPT cannot {action}.")
        model = payload.get("model") or self.setting("model")
        if not model:
            offered = self.models()
            if not offered:
                raise ConnectorError("This ChatGPT plan offers no models to other apps.")
            model = offered[0][0]
        body = {
            "model": model,
            "input": [item for message in payload["messages"] for item in to_items(message)],
            "store": False,
            "stream": True,
        }
        if payload.get("system"):
            body["instructions"] = payload["system"]
        if payload.get("tools"):
            body["tools"] = [
                {
                    "type": "namespace",
                    "name": NAMESPACE,
                    "description": "Tools for looking things up in the CRM and proposing changes.",
                    "tools": [
                        {
                            "type": "function",
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["parameters"],
                        }
                        for tool in payload["tools"]
                    ],
                }
            ]
        if payload.get("schema"):
            body["text"] = text_format(payload)
        answer = read_stream(
            self.call("/responses", json=body, headers={"Accept": "text/event-stream"}).text
        )
        return read_response(answer, payload, model, "ChatGPT")
