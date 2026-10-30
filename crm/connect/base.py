"""What a connector declares and the behavior every connector provides."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import models


class Capability(models.TextChoices):
    PHONE = "phone", "Phone"
    TEXTING = "texting", "Texting and messaging"
    MAILBOX = "mailbox", "Mailbox"
    SYSTEM_EMAIL = "system_email", "System email"
    CALENDAR = "calendar", "Calendar"
    SCHEDULING = "scheduling", "Scheduling"
    VIDEO = "video", "Video meetings"
    PAYMENTS = "payments", "Payments"
    ACCOUNTING = "accounting", "Accounting"
    STORAGE = "storage", "Document storage"
    ESIGNATURE = "esignature", "E-signature"
    AI_MODELS = "ai_models", "AI models"
    TRANSLATION = "translation", "Translation"
    LEGAL_RESEARCH = "legal_research", "Legal research"
    CASE_STATUS = "case_status", "Case status"
    SHIPPING = "shipping", "Shipping"
    LEAD_CAPTURE = "lead_capture", "Lead capture"
    MARKETING = "marketing", "Marketing analytics"
    SIGN_IN = "sign_in", "Sign-in (SSO)"
    MIGRATION = "migration", "Migration"
    AUTOMATION = "automation", "Automation"


# How "Connect your ..." prompts name each capability.
CAPABILITY_NOUNS = {
    Capability.PHONE: "phone system",
    Capability.TEXTING: "texting service",
    Capability.MAILBOX: "mailbox",
    Capability.SYSTEM_EMAIL: "email delivery service",
    Capability.CALENDAR: "calendar",
    Capability.SCHEDULING: "scheduling tool",
    Capability.VIDEO: "video meeting service",
    Capability.PAYMENTS: "payment processor",
    Capability.ACCOUNTING: "accounting software",
    Capability.STORAGE: "document storage",
    Capability.ESIGNATURE: "e-signature service",
    Capability.AI_MODELS: "AI model",
    Capability.TRANSLATION: "translation service",
    Capability.LEGAL_RESEARCH: "legal research service",
    Capability.CASE_STATUS: "case status source",
    Capability.SHIPPING: "shipping carrier",
    Capability.LEAD_CAPTURE: "lead sources",
    Capability.MARKETING: "marketing accounts",
    Capability.SIGN_IN: "sign-in provider",
    Capability.MIGRATION: "previous practice system",
    Capability.AUTOMATION: "automation tool",
}


class Maturity(models.TextChoices):
    READY = "ready", "Ready"
    BETA = "beta", "Beta"
    WANTED = "wanted", "Wanted"


class ConnectionScope(models.TextChoices):
    FIRM = "firm", "Whole firm"
    USER = "user", "Each staff member"


@dataclass(frozen=True)
class CredentialField:
    """One value staff enter when connecting.

    ``generate`` makes the CRM propose a fresh value, such as a signing secret
    for staff to copy into the provider. It is shown only on the connect form.
    """

    name: str
    label: str
    secret: bool = True
    help_text: str = ""
    generate: Callable[[], str] | None = None
    # False for a value some setups do without, such as a key a local server ignores.
    required: bool = True


@dataclass(frozen=True)
class SettingField:
    """A non-secret choice made when connecting, kept in the connection's ``config``.

    With ``choices`` staff pick from a list; otherwise they type a value, which
    may be left blank unless ``required``. ``validate`` may raise ``ValidationError``
    for a value the connector cannot use; it is not called for a blank value.

    ``options(connector)`` returns the choices live instead, such as the event types
    at the provider; such a setting is chosen on the connection's page once it is
    connected. With ``multiple``, staff pick any number and the value is a list.
    """

    name: str
    label: str
    choices: tuple[tuple[str, str], ...] = ()
    default: str = ""
    required: bool = True
    help_text: str = ""
    validate: Callable[[str], None] | None = None
    options: Callable[["Connector"], list[tuple[str, str]]] | None = None
    multiple: bool = False


@dataclass(frozen=True)
class ApiKeyAuth:
    """Staff paste one or more values from the provider's settings."""

    fields: tuple[CredentialField, ...]
    docs_url: str = ""
    # How the catalog names this kind of setup, such as "Webhook address".
    label: str = "API key"


@dataclass(frozen=True)
class OAuthAuth:
    """Staff sign in at the provider; scopes are requested per enabled capability.

    ``app_fields`` are non-secret values a superuser enters with the firm's app
    registration, such as a directory (tenant) ID. The URLs may name them in braces,
    such as ``https://login.example.test/{tenant}/authorize``.
    """

    authorize_url: str
    token_url: str
    scopes: dict[str, tuple[str, ...]]
    docs_url: str = ""
    # Provider-specific authorize parameters, such as a request for offline access.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    app_fields: tuple["SettingField", ...] = ()


@dataclass(frozen=True)
class KeyExchangeAuth:
    """Staff sign in at the provider, which hands back an API key; there is no app to register.

    A PKCE sign-in, such as OpenRouter's. The connector builds the sign-in address
    with ``authorize_query()`` and turns the returned code into credentials with
    ``exchange_code()``.
    """

    authorize_url: str
    docs_url: str = ""


SIGN_IN_TYPES = (OAuthAuth, KeyExchangeAuth)


@dataclass(frozen=True)
class PublicDataAuth:
    """Public data that needs no credentials, only a decision to turn it on."""


AUTH_TYPES = (ApiKeyAuth, OAuthAuth, KeyExchangeAuth, PublicDataAuth)


@dataclass(frozen=True)
class DataFlows:
    """Plain-language description of what leaves and enters the CRM.

    Raise ``version`` whenever the description changes so firms acknowledge it again.
    """

    version: int
    sends: tuple[str, ...] = ()
    receives: tuple[str, ...] = ()


@dataclass(frozen=True)
class Record:
    """A provider record normalized enough for core to store or match."""

    external_id: str
    kind: str
    occurred_at: datetime
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SyncBatch:
    records: list[Record]
    cursor: dict
    # Set when the sync stopped early but kept what it received, such as at a rate
    # limit. It is shown to staff, so it must not contain client data.
    warning: str = ""


WEBHOOK_SECRET_SOURCES = ("crm", "provider", "credential")


@dataclass(frozen=True)
class WebhookSpec:
    """How a provider delivers events to the connection's private /hooks/ URL.

    ``secret_source`` is "crm" when the CRM generates the signing secret for staff
    to paste at the provider, "provider" when staff paste the provider's secret
    into the CRM, or "credential" when the provider signs with one of the
    connection's credentials, named by ``secret_credential``.

    ``register_label`` offers to set the address at the provider when webhooks are
    turned on, through ``Connector.register_webhook()``; it says what that changes.
    ``ack_status``, ``ack_body``, and ``ack_content_type`` are the answer to an
    accepted event, for providers that read it, such as one that would text a
    plain-text answer back.
    """

    capabilities: tuple[str, ...]
    secret_source: str = "crm"
    secret_credential: str = ""
    instructions: str = ""
    register_label: str = ""
    ack_status: int = 200
    ack_body: str = "ok"
    ack_content_type: str = "text/plain"


@dataclass(frozen=True)
class WebhookEnvelope:
    """The minimum of a verified webhook that processing needs; stored encrypted."""

    event_id: str
    capability: str
    payload: dict


class ConnectorError(Exception):
    """A provider failure. The message is stored and shown, so it must not contain client data.

    ``status`` is the provider's HTTP status, when there was one.
    """

    def __init__(self, message="", *, status=None):
        super().__init__(message)
        self.status = status


class Connector:
    """Base class for connectors. Subclasses declare their manifest as class attributes."""

    # Templates receive connector classes; without this Django would try to call them.
    do_not_call_in_templates = True

    key = ""
    name = ""
    description = ""
    maturity = Maturity.BETA
    capabilities: frozenset[str] = frozenset()
    auth: ApiKeyAuth | OAuthAuth | PublicDataAuth | None = None
    settings_fields: tuple[SettingField, ...] = ()
    connection_scope = ConnectionScope.FIRM
    data_flows: DataFlows | None = None
    maintainers: tuple[str, ...] = ()
    # Outbound action name -> the capability it belongs to.
    outbound_actions: dict[str, str] = {}
    # Actions that only keep a personal connection's own account in step with the CRM,
    # such as putting its owner's appointments on their own calendar. They may run
    # automatically, but only for records assigned to the connection's owner.
    owner_sync_actions: frozenset[str] = frozenset()
    # Capabilities the worker pulls on a schedule, and how often.
    sync_capabilities: frozenset[str] = frozenset()
    sync_interval = timedelta(minutes=15)
    webhooks: WebhookSpec | None = None
    # For legal research: what a search can cover, as (value, label) pairs, the first
    # being the default. Core passes the chosen value as the search's ``scope``.
    search_scopes: tuple[tuple[str, str], ...] = ()
    # Hidden connectors (such as the fictional example) stay out of the catalog.
    hidden = False

    def __init__(self, connection):
        self.connection = connection

    @property
    def credentials(self):
        return self.connection.credentials or {}

    def setting(self, name):
        """The connection's value for one of ``settings_fields``, or that field's default."""
        field = next(f for f in self.settings_fields if f.name == name)
        default = [] if field.multiple else field.default
        return (self.connection.config or {}).get(name, default)

    def access_token(self):
        """For OAuth connectors: a current access token, refreshed when it is about to expire."""
        from .oauth import access_token

        return access_token(self.connection)

    def test_connection(self):
        """Raise ConnectorError unless the stored credentials work."""
        raise NotImplementedError

    def sync(self, capability, cursor):
        """Return a SyncBatch of records newer than ``cursor`` for one capability."""
        raise NotImplementedError

    def perform(self, action, payload):
        """Carry out an outbound action and return the provider's reference for it.

        Call ``crm.connect.policy.send`` instead of this method; it applies the
        outbound policy and records the attempt.
        """
        raise NotImplementedError

    def revoke(self):
        """Revoke the stored credentials at the provider, if it supports that."""

    def verify_webhook(self, body, headers, secret, *, url=""):
        """Return True only if ``body`` carries a valid provider signature made with ``secret``.

        ``url`` is the address the request came to, for providers that sign it.
        Use a constant-time comparison. Never accept an unsigned request.
        """
        raise NotImplementedError

    def authorize_query(self, callback_url, code_challenge, state):
        """For KeyExchangeAuth: the sign-in address's query parameters."""
        raise NotImplementedError

    def exchange_code(self, code, code_verifier, callback_url):
        """For KeyExchangeAuth: the credentials to store for a returned sign-in code."""
        raise NotImplementedError

    def webhook_challenge(self, url, body, headers):
        """Answer a provider's check that the address is ours, before any verification.

        Return ``(status, text)`` to answer with plain text, or None for a normal
        event. Echo only what the provider sent, and never act on it.
        """
        return None

    def register_webhook(self, url, secret):
        """Set ``url`` as the provider's delivery address for this connection.

        Only for connectors whose ``webhooks.register_label`` offers it. Return a
        short note for staff, or raise ConnectorError.
        """
        raise NotImplementedError

    def parse_webhook(self, body, headers):
        """Return a WebhookEnvelope holding only what processing needs.

        Called after verification. Raise ConnectorError for a malformed event.
        """
        raise NotImplementedError

    def handle_webhook(self, capability, payload):
        """Turn a stored webhook payload into Records. Runs in the worker."""
        raise NotImplementedError
