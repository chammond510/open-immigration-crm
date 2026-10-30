"""OpenRouter: many models through one account, connected by signing in.

A superuser signs in at OpenRouter (PKCE), which hands back an API key for the
firm's account; there is no app to register. Requests use OpenRouter's
OpenAI-compatible chat API and always ask to be routed only to providers that do
not store or train on the data, or, with the stricter setting, only to providers
with zero data retention.

OpenRouter has no way for a key to revoke itself, so disconnecting deletes the key
here; delete it at openrouter.ai/keys too.

API reference: https://openrouter.ai/docs
"""

from connectors.openai_compatible import chat_body, chat_reply
from crm.connect.base import (
    Capability,
    Connector,
    ConnectorError,
    DataFlows,
    KeyExchangeAuth,
    Maturity,
    SettingField,
)
from crm.connect.http import ProviderClient
from crm.connect.registry import register

API_URL = "https://openrouter.ai/api/v1"
TITLE = "Open Immigration CRM"


@register
class OpenRouterConnector(Connector):
    key = "openrouter"
    name = "OpenRouter"
    description = (
        "Power the CRM's AI features with models from many providers through the firm's "
        "OpenRouter account, connected by signing in."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AI_MODELS})
    auth = KeyExchangeAuth(
        authorize_url="https://openrouter.ai/auth",
        docs_url="https://openrouter.ai/docs/guides/overview/auth/oauth",
    )
    settings_fields = (
        SettingField(
            "model",
            "Model",
            help_text="An OpenRouter model ID, such as the ones listed at openrouter.ai/models.",
        ),
        SettingField(
            "privacy",
            "Providers allowed",
            choices=(
                ("deny", "Only providers that do not store or train on the data"),
                ("zdr", "Only providers with zero data retention"),
            ),
            default="deny",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "What each AI feature turned on in AI settings sends, through OpenRouter to the "
            "model provider it routes to",
        ),
        receives=("The model's answers",),
    )
    outbound_actions = {"generate": Capability.AI_MODELS}

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="OpenRouter",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
                "X-OpenRouter-Title": TITLE,
            },
            timeout=300,
        )

    def authorize_query(self, callback_url, code_challenge, state):
        return {
            "callback_url": callback_url,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }

    def exchange_code(self, code, code_verifier, callback_url):
        answer = ProviderClient(API_URL, name="OpenRouter", retries=1).post(
            "/auth/keys",
            json={"code": code, "code_verifier": code_verifier, "code_challenge_method": "S256"},
        )
        if not answer.get("key"):
            raise ConnectorError("OpenRouter did not return a key.")
        return {"api_key": answer["key"]}

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError(
                    "OpenRouter did not accept the key. Sign in again.", status=401
                ) from None
            if exc.status == 402:
                raise ConnectorError(
                    "The OpenRouter account is out of credits.", status=402
                ) from None
            raise

    def test_connection(self):
        self.call("GET", "/key")

    def perform(self, action, payload):
        if action != "generate":
            raise ConnectorError(f"OpenRouter cannot {action}.")
        model = payload.get("model") or self.setting("model")
        if not model:
            raise ConnectorError("Enter the model on the connection's page.")
        body = chat_body(payload, model)
        provider = {"data_collection": "deny"}
        if self.setting("privacy") == "zdr":
            provider["zdr"] = True
        if payload.get("schema"):
            # Route only to providers that honour the requested format.
            provider["require_parameters"] = True
        body["provider"] = provider
        answer = self.call("POST", "/chat/completions", json=body)
        return chat_reply(answer, model, schema=payload.get("schema"))
