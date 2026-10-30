"""What a connector declares and the behavior every connector provides."""

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


class Maturity(models.TextChoices):
    READY = "ready", "Ready"
    BETA = "beta", "Beta"
    WANTED = "wanted", "Wanted"


class ConnectionScope(models.TextChoices):
    FIRM = "firm", "Whole firm"
    USER = "user", "Each staff member"


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = True
    help_text: str = ""


@dataclass(frozen=True)
class ApiKeyAuth:
    """Staff paste one or more values from the provider's settings."""

    fields: tuple[CredentialField, ...]
    docs_url: str = ""


@dataclass(frozen=True)
class OAuthAuth:
    """Staff sign in at the provider; scopes are requested per enabled capability."""

    authorize_url: str
    token_url: str
    scopes: dict[str, tuple[str, ...]]
    docs_url: str = ""


@dataclass(frozen=True)
class PublicDataAuth:
    """Public data that needs no credentials, only a decision to turn it on."""


AUTH_TYPES = (ApiKeyAuth, OAuthAuth, PublicDataAuth)


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


WEBHOOK_SECRET_SOURCES = ("crm", "provider")


@dataclass(frozen=True)
class WebhookSpec:
    """How a provider delivers events to the connection's private /hooks/ URL.

    ``secret_source`` is "crm" when the CRM generates the signing secret for staff
    to paste at the provider, or "provider" when staff paste the provider's secret
    into the CRM.
    """

    capabilities: tuple[str, ...]
    secret_source: str = "crm"
    instructions: str = ""


@dataclass(frozen=True)
class WebhookEnvelope:
    """The minimum of a verified webhook that processing needs; stored encrypted."""

    event_id: str
    capability: str
    payload: dict


class ConnectorError(Exception):
    """A provider failure. The message is stored and shown, so it must not contain client data."""


class Connector:
    """Base class for connectors. Subclasses declare their manifest as class attributes."""

    key = ""
    name = ""
    description = ""
    maturity = Maturity.BETA
    capabilities: frozenset[str] = frozenset()
    auth: ApiKeyAuth | OAuthAuth | PublicDataAuth | None = None
    connection_scope = ConnectionScope.FIRM
    data_flows: DataFlows | None = None
    maintainers: tuple[str, ...] = ()
    # Outbound action name -> the capability it belongs to.
    outbound_actions: dict[str, str] = {}
    # Capabilities the worker pulls on a schedule, and how often.
    sync_capabilities: frozenset[str] = frozenset()
    sync_interval = timedelta(minutes=15)
    webhooks: WebhookSpec | None = None
    # Hidden connectors (such as the fictional example) stay out of the catalog.
    hidden = False

    def __init__(self, connection):
        self.connection = connection

    @property
    def credentials(self):
        return self.connection.credentials or {}

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

    def verify_webhook(self, body, headers, secret):
        """Return True only if ``body`` carries a valid provider signature made with ``secret``.

        Use a constant-time comparison. Never accept an unsigned request.
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
