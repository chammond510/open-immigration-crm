import hashlib
import secrets

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.models import AuditLog

from .base import ApiKeyAuth, Capability, ConnectionScope, ConnectorError
from .models import Connection
from .registry import registry


def record_audit(actor, action, connection, detail=""):
    AuditLog.objects.create(
        user=actor,
        action=action,
        target_type="Connection",
        target_id=str(connection.pk),
        detail=detail[:500],
    )


def clean_settings(cls, config):
    """The connector's settings from ``config``, with defaults filled in and values checked."""
    cleaned = dict(config)
    for setting in cls.settings_fields:
        if setting.options or setting.multiple:
            # Chosen from live options on the connection's page, which checks them.
            default = [] if setting.multiple else setting.default
            cleaned[setting.name] = config.get(setting.name, default)
            continue
        value = str(config.get(setting.name, setting.default)).strip()
        if setting.choices and value not in dict(setting.choices):
            raise ValidationError(f"Choose a valid {setting.label.lower()}.")
        if not value and setting.required and not setting.choices:
            raise ValidationError(f"Enter the {setting.label.lower()}.")
        if setting.validate and value:
            setting.validate(value)
        cleaned[setting.name] = value
    return cleaned


def create_connection(
    connector_key, *, label, created_by, capabilities, credentials=None, owner=None, config=None
):
    """Create a connection after checking it against the connector's manifest."""
    cls = registry.get(connector_key)
    if cls is None:
        raise ValidationError(f"No connector named {connector_key!r} is installed.")
    capabilities = sorted(set(capabilities))
    if not capabilities or not set(capabilities) <= set(cls.capabilities):
        raise ValidationError("Choose capabilities this connector provides.")
    if cls.connection_scope == ConnectionScope.USER and owner is None:
        raise ValidationError(f"{cls.name} connects one staff member at a time; set an owner.")
    if cls.connection_scope == ConnectionScope.FIRM and owner is not None:
        raise ValidationError(f"{cls.name} connects for the whole firm; leave the owner empty.")
    if isinstance(cls.auth, ApiKeyAuth):
        missing = [
            f.label for f in cls.auth.fields if f.required and not (credentials or {}).get(f.name)
        ]
        if missing:
            raise ValidationError(f"Missing: {', '.join(missing)}.")
    connection = Connection.objects.create(
        connector_key=connector_key,
        label=label,
        owner=owner,
        enabled_capabilities=capabilities,
        credentials=credentials,
        config=clean_settings(cls, config or {}),
        created_by=created_by,
    )
    record_audit(created_by, "connection.created", connection, cls.name)
    return connection


def acknowledge_data_flows(connection, user):
    """Record that ``user`` reviewed what this connection sends and receives."""
    cls = connection.connector_class
    if cls is None:
        raise ValidationError("This connection's connector is not installed.")
    connection.data_flows_version = cls.data_flows.version
    connection.data_flows_acknowledged_by = user
    connection.data_flows_acknowledged_at = timezone.now()
    connection.save(
        update_fields=[
            "data_flows_version",
            "data_flows_acknowledged_by",
            "data_flows_acknowledged_at",
            "updated_at",
        ]
    )
    record_audit(
        user, "connection.data_flows_acknowledged", connection, f"v{cls.data_flows.version}"
    )


def check_connection(connection):
    """Run the connector's credential test and record the result on the connection."""
    connector = connection.connector()
    if connector is None:
        raise ValidationError("This connection's connector is not installed.")
    try:
        connector.test_connection()
    except ConnectorError as exc:
        connection.status = Connection.Status.NEEDS_ATTENTION
        connection.last_error_at = timezone.now()
        connection.last_error = str(exc)[:500]
        connection.save(update_fields=["status", "last_error_at", "last_error", "updated_at"])
        return False
    connection.status = Connection.Status.ACTIVE
    connection.last_success_at = timezone.now()
    connection.last_error = ""
    connection.save(update_fields=["status", "last_success_at", "last_error", "updated_at"])
    return True


@transaction.atomic
def disconnect(connection, actor):
    """Revoke where possible, then delete stored credentials. Synced records are kept."""
    connector = connection.connector()
    revoke_note = ""
    if connector is not None:
        try:
            connector.revoke()
        except ConnectorError as exc:
            revoke_note = f"Provider revocation failed: {str(exc)[:200]}"
    connection.credentials = None
    connection.granted_scopes = []
    connection.webhook_token_hash = None
    connection.webhook_secret = None
    connection.status = Connection.Status.DISCONNECTED
    connection.save(
        update_fields=[
            "credentials",
            "granted_scopes",
            "webhook_token_hash",
            "webhook_secret",
            "status",
            "updated_at",
        ]
    )
    record_audit(actor, "connection.disconnected", connection, revoke_note)
    return connection


def enable_webhooks(connection, actor, provider_secret="", *, registering=False):
    """Issue a new private /hooks/ URL token and signing secret; return both to show once.

    For connectors whose provider issues the signing secret, pass it as
    ``provider_secret``. When the CRM is registering the address at such a provider
    (``registering``), it may be left out: the connector's ``register_webhook()``
    stores the secret the provider returns. Calling this again rotates the token and
    secret.
    """
    cls = connection.connector_class
    if cls is None or cls.webhooks is None:
        raise ValidationError("This connection does not receive webhooks.")
    registers = registering and bool(cls.webhooks.register_label)
    if cls.webhooks.secret_source == "provider" and not provider_secret and not registers:
        raise ValidationError(f"Paste the signing secret {cls.name} shows for this webhook.")
    if cls.webhooks.secret_source == "credential":
        # The provider signs with a credential staff already entered, such as an auth token.
        provider_secret = (connection.credentials or {}).get(cls.webhooks.secret_credential, "")
        if not provider_secret:
            raise ValidationError(
                "Connect again to enter the credentials webhooks are signed with."
            )
    token = secrets.token_urlsafe(32)
    secret = provider_secret or secrets.token_urlsafe(32)
    connection.webhook_token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection.webhook_secret = secret
    connection.save(update_fields=["webhook_token_hash", "webhook_secret", "updated_at"])
    record_audit(actor, "connection.webhooks_enabled", connection)
    return token, secret


def can_manage(user, connection):
    """Superusers manage every connection; staff manage only their own personal ones."""
    return user.is_superuser or (connection.owner_id is not None and connection.owner_id == user.pk)


def visible_connections(user):
    """Firm-wide connections plus the user's own; superusers see every connection."""
    connections = Connection.objects.select_related("owner")
    if user.is_superuser:
        return connections
    return connections.filter(Q(owner__isnull=True) | Q(owner=user))


def usable_connections(user, action, capability):
    """Active connections with this outbound action that ``user`` may use: firm and own."""
    found = []
    for connection in visible_connections(user).filter(status=Connection.Status.ACTIVE):
        cls = connection.connector_class
        if (
            cls is not None
            and capability in connection.enabled_capabilities
            and cls.outbound_actions.get(action) == capability
            and connection.owner_id in (None, user.pk)
        ):
            found.append(connection)
    return found


def needs_worker(connection):
    """Syncs, received webhooks, and automation event deliveries all run in the worker."""
    cls = connection.connector_class
    return cls is not None and bool(
        cls.sync_capabilities or cls.webhooks or Capability.AUTOMATION in cls.capabilities
    )


def connection_alerts(user):
    """Problems a staff member should see: connections that need attention, a stopped worker."""
    from .scheduler import worker_is_running

    active = visible_connections(user).exclude(status=Connection.Status.DISCONNECTED)
    troubled = [
        connection
        for connection in active
        if connection.status in (Connection.Status.NEEDS_ATTENTION, Connection.Status.NEEDS_REAUTH)
    ]
    worker_needed = any(
        connection.status == Connection.Status.ACTIVE and needs_worker(connection)
        for connection in active
    )
    return {
        "troubled": troubled,
        "worker_stopped": worker_needed and not worker_is_running(),
    }
