"""Staff pages for browsing the catalog and managing connections."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.models import AuditLog

from . import oauth, sign_in
from .base import (
    SIGN_IN_TYPES,
    ApiKeyAuth,
    Capability,
    ConnectionScope,
    ConnectorError,
    Maturity,
    OAuthAuth,
)
from .forms import (
    ConnectForm,
    ConnectionConfigForm,
    ConnectionSettingsForm,
    ConnectorAppForm,
    ProviderSecretForm,
)
from .models import Connection, ConnectorApp
from .registry import registry
from .scheduler import worker_is_running
from .services import (
    acknowledge_data_flows,
    can_manage,
    check_connection,
    create_connection,
    disconnect,
    enable_webhooks,
    needs_worker,
    record_audit,
    visible_connections,
)

staff_required = user_passes_test(
    lambda user: user.is_active and user.is_staff,
    login_url="login",
)


def require_superuser(user):
    if not user.is_superuser:
        raise PermissionDenied


def monogram(name):
    words = [word for word in name.replace("-", " ").split() if word[:1].isalnum()]
    return "".join(word[0] for word in words[:2]).upper() or "?"


def auth_label(cls):
    if isinstance(cls.auth, SIGN_IN_TYPES):
        return "Sign in"
    if isinstance(cls.auth, ApiKeyAuth):
        return cls.auth.label
    return "No sign-in"


def setting_values(connection, form=None):
    """(label, value) for each of the connector's settings, with choices shown by name.

    Live options are named from the settings form, which has loaded them.
    """
    cls = connection.connector_class
    config = connection.config or {}
    values = []
    for setting in cls.settings_fields if cls else ():
        value = config.get(setting.name, setting.default)
        field = form.fields.get(f"setting_{setting.name}") if form else None
        names = dict(getattr(field, "choices", None) or setting.choices)
        if isinstance(value, list):
            shown = (
                ", ".join(str(names.get(v, v)) for v in value) if field else f"{len(value)} chosen"
            )
            values.append((setting.label, shown or "None chosen"))
        elif value:
            values.append((setting.label, names.get(value, value)))
    return values


def catalog_entry(entry, connected_keys):
    return {
        "entry": entry,
        "works_through": works_through(entry),
        "monogram": monogram(entry.name),
        "connected": entry.key in connected_keys,
        "auth": auth_label(entry.connector) if entry.connector else "",
        "test_only": bool(entry.connector and entry.connector.hidden),
        "maturity_label": Maturity(entry.maturity).label,
    }


def works_through(entry):
    """The installed connector a wanted provider already works through, or None."""
    cls = registry.get(entry.works_through) if entry.works_through else None
    return cls if cls is not None and not cls.hidden else None


def connector_or_404(key, *, include_wanted=False):
    entry = next((item for item in registry.catalog(include_hidden=True) if item.key == key), None)
    if entry is None:
        raise Http404
    cls = entry.connector
    if cls is not None and cls.hidden and not settings.CONNECTORS_SHOW_HIDDEN:
        raise Http404
    if cls is None and not include_wanted:
        raise Http404
    return entry, cls


def managed_connection_or_404(request, pk):
    connection = get_object_or_404(visible_connections(request.user), pk=pk)
    if not can_manage(request.user, connection):
        raise PermissionDenied
    return connection


@staff_required
@require_GET
def catalog(request):
    query = request.GET.get("q", "").strip()
    selected_capability = request.GET.get("capability", "")
    selected_maturity = request.GET.get("maturity", "")
    connections = list(
        visible_connections(request.user).exclude(status=Connection.Status.DISCONNECTED)
    )
    connected_keys = {connection.connector_key for connection in connections}
    entries = [
        entry
        for entry in registry.catalog(include_hidden=settings.CONNECTORS_SHOW_HIDDEN)
        if (not query or query.lower() in entry.name.lower())
        and (not selected_maturity or entry.maturity == selected_maturity)
    ]
    groups = []
    for key, label in Capability.choices:
        if selected_capability and key != selected_capability:
            continue
        tiles = [catalog_entry(e, connected_keys) for e in entries if key in e.capabilities]
        if tiles:
            groups.append({"key": key, "label": label, "tiles": tiles})
    worker_stopped = (
        any(
            connection.status == Connection.Status.ACTIVE and needs_worker(connection)
            for connection in connections
        )
        and not worker_is_running()
    )
    return render(
        request,
        "connect/catalog.html",
        {
            "connections": connections,
            "groups": groups,
            "query": query,
            "capabilities": Capability.choices,
            "maturities": Maturity.choices,
            "selected_capability": selected_capability,
            "selected_maturity": selected_maturity,
            "worker_stopped": worker_stopped,
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def connector_detail(request, key):
    entry, cls = connector_or_404(key, include_wanted=True)
    context = {
        "entry": entry,
        "connector": cls,
        "monogram": monogram(entry.name),
        "maturity_label": Maturity(entry.maturity).label,
        "works_through": works_through(entry),
    }
    if cls is None:
        return render(request, "connect/connector_detail.html", context)
    if Capability.SIGN_IN in cls.capabilities:
        context["sign_in_callback"] = sign_in.callback_url(request)

    is_oauth = isinstance(cls.auth, SIGN_IN_TYPES)
    needs_app = isinstance(cls.auth, OAuthAuth)
    app = ConnectorApp.objects.filter(connector_key=key).first() if needs_app else None
    may_connect = request.user.is_superuser or cls.connection_scope == ConnectionScope.USER
    form = None
    if may_connect and (app or not needs_app):
        form = ConnectForm(request.POST or None, connector=cls, user=request.user)
    if request.method == "POST":
        if form is None:
            raise PermissionDenied
        if form.is_valid():
            try:
                connection = create_connection(
                    key,
                    label=form.cleaned_data["label"],
                    created_by=request.user,
                    capabilities=form.cleaned_data["capabilities"],
                    credentials=form.credentials() if not is_oauth else None,
                    owner=request.user if form.personal else None,
                    config=form.settings(),
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                if form.cleaned_data.get("acknowledge"):
                    acknowledge_data_flows(connection, request.user)
                if is_oauth:
                    connection.status = Connection.Status.NEEDS_REAUTH
                    connection.save(update_fields=["status", "updated_at"])
                    messages.info(request, f"Now sign in to {cls.name} to finish connecting.")
                elif check_connection(connection):
                    messages.success(request, f"Connected to {cls.name}.")
                else:
                    messages.error(request, f"{cls.name} did not accept those credentials.")
                return redirect("connection_detail", pk=connection.pk)

    context.update(
        {
            "form": form,
            "may_connect": may_connect,
            "is_oauth": is_oauth,
            "needs_app": needs_app,
            "app": app,
            "app_form": ConnectorAppForm(
                initial={"client_id": app.client_id, **app.config} if app else None,
                connector=cls,
            ),
            "callback_url": oauth.callback_url(request) if is_oauth else "",
            "capability_labels": [Capability(c).label for c in sorted(cls.capabilities)],
            "auth_label": auth_label(cls),
        }
    )
    return render(request, "connect/connector_detail.html", context)


@staff_required
@require_POST
def connector_app(request, key):
    require_superuser(request.user)
    _, cls = connector_or_404(key)
    if not isinstance(cls.auth, OAuthAuth):
        raise Http404
    form = ConnectorAppForm(request.POST, connector=cls)
    if form.is_valid():
        ConnectorApp.objects.update_or_create(
            connector_key=key,
            defaults={
                "client_id": form.cleaned_data["client_id"].strip(),
                "client_secret": form.cleaned_data["client_secret"].strip(),
                "config": form.app_config(),
                "updated_by": request.user,
            },
        )
        AuditLog.objects.create(
            user=request.user,
            action="connector_app.saved",
            target_type="ConnectorApp",
            target_id=key,
        )
        messages.success(request, f"Saved the firm's {cls.name} app details.")
    else:
        problems = [e for errors in form.errors.values() for e in errors]
        messages.error(request, " ".join(problems) or "Enter the app's details.")
    return redirect("connector_detail", key=key)


@staff_required
@require_GET
def connection_detail(request, pk):
    connection = get_object_or_404(visible_connections(request.user), pk=pk)
    cls = connection.connector_class
    is_oauth = cls is not None and isinstance(cls.auth, SIGN_IN_TYPES)
    webhooks = cls.webhooks if cls else None
    context = {
        "connection": connection,
        "connector": cls,
        "monogram": monogram(cls.name if cls else connection.label),
        "can_manage": can_manage(request.user, connection),
        "is_oauth": is_oauth,
        "loopback": is_oauth and bool(getattr(cls.auth, "loopback_redirect", "")),
        "signed_in": bool(connection.credentials),
        "webhooks": webhooks,
        "webhooks_on": bool(connection.webhook_token_hash),
        "provider_secret_form": ProviderSecretForm()
        if webhooks and webhooks.secret_source == "provider"
        else None,
        "settings_form": ConnectionSettingsForm(
            initial={
                "preview_mode": connection.preview_mode,
                "outbound_admin_only": connection.outbound_admin_only,
            }
        ),
        "capability_labels": [Capability(c).label for c in connection.enabled_capabilities],
        "config_form": (form := config_form(request, connection)),
        "settings": setting_values(connection, form),
        "sync_runs": connection.sync_runs.all()[:5],
        "webhook_events": connection.webhook_events.all()[:5],
        "outbound_requests": connection.outbound_requests.select_related("actor")[:5],
        "worker_stopped": connection.status == Connection.Status.ACTIVE
        and needs_worker(connection)
        and not worker_is_running(),
        "disconnected": connection.status == Connection.Status.DISCONNECTED,
    }
    return render(request, "connect/connection_detail.html", context)


@staff_required
@require_POST
def connection_acknowledge(request, pk):
    require_superuser(request.user)
    connection = managed_connection_or_404(request, pk)
    acknowledge_data_flows(connection, request.user)
    messages.success(request, "Recorded your review of what this connection sends and receives.")
    return redirect("connection_detail", pk=pk)


@staff_required
@require_POST
def connection_test(request, pk):
    connection = managed_connection_or_404(request, pk)
    if check_connection(connection):
        messages.success(request, "The connection test passed.")
    else:
        messages.error(request, f"The connection test failed: {connection.last_error}")
    return redirect("connection_detail", pk=pk)


@staff_required
@require_POST
def connection_settings(request, pk):
    require_superuser(request.user)
    connection = managed_connection_or_404(request, pk)
    form = ConnectionSettingsForm(request.POST)
    if form.is_valid():
        connection.preview_mode = form.cleaned_data["preview_mode"]
        connection.outbound_admin_only = form.cleaned_data["outbound_admin_only"]
        connection.save(update_fields=["preview_mode", "outbound_admin_only", "updated_at"])
        record_audit(
            request.user,
            "connection.settings_changed",
            connection,
            f"preview {'on' if connection.preview_mode else 'off'}, "
            f"admin-only {'on' if connection.outbound_admin_only else 'off'}",
        )
        messages.success(request, "Connection settings saved.")
    return redirect("connection_detail", pk=pk)


def config_form(request, connection, data=None):
    """The connection's settings form, for those who manage it, while it is connected."""
    cls = connection.connector_class
    if (
        cls is None
        or not cls.settings_fields
        or not can_manage(request.user, connection)
        or connection.status == Connection.Status.DISCONNECTED
    ):
        return None
    return ConnectionConfigForm(data, connector=connection.connector())


@staff_required
@require_POST
def connection_config(request, pk):
    connection = managed_connection_or_404(request, pk)
    form = config_form(request, connection, request.POST)
    if form is None:
        raise Http404
    if form.is_valid():
        connection.config = {**(connection.config or {}), **form.settings()}
        connection.save(update_fields=["config", "updated_at"])
        record_audit(request.user, "connection.config_changed", connection)
        messages.success(request, "Settings saved.")
    else:
        problems = [e for errors in form.errors.values() for e in errors]
        messages.error(request, "Settings not saved: " + " ".join(problems))
    return redirect("connection_detail", pk=pk)


@staff_required
@require_POST
def connection_webhooks(request, pk):
    connection = managed_connection_or_404(request, pk)
    cls = connection.connector_class
    provider_secret = ""
    if cls and cls.webhooks and cls.webhooks.secret_source == "provider":
        form = ProviderSecretForm(request.POST)
        provider_secret = form.data.get("provider_secret", "").strip()
    try:
        token, secret = enable_webhooks(
            connection,
            request.user,
            provider_secret,
            registering=request.POST.get("register") == "on",
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("connection_detail", pk=pk)
    webhook_url = request.build_absolute_uri(reverse("connector_webhook", args=[token]))
    registered, register_error = "", ""
    if cls.webhooks.register_label and request.POST.get("register") == "on":
        try:
            registered = connection.connector().register_webhook(webhook_url, secret) or "Done."
        except ConnectorError as exc:
            register_error = str(exc)
        record_audit(
            request.user,
            "connection.webhook_registered",
            connection,
            "registered" if not register_error else f"failed: {register_error}",
        )
    return render(
        request,
        "connect/webhook_details.html",
        {
            "connection": connection,
            "connector": cls,
            "webhook_url": webhook_url,
            "secret": "" if cls.webhooks.secret_source != "crm" else secret,
            "registered": registered,
            "register_error": register_error,
        },
    )


@staff_required
@require_POST
def connection_disconnect(request, pk):
    connection = managed_connection_or_404(request, pk)
    disconnect(connection, request.user)
    messages.success(request, "Disconnected. Stored credentials were deleted.")
    return redirect("connection_detail", pk=pk)


@staff_required
@require_GET
def connection_oauth_start(request, pk):
    connection = managed_connection_or_404(request, pk)
    if connection.status == Connection.Status.DISCONNECTED:
        raise Http404
    try:
        return redirect(oauth.authorization_url(request, connection))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("connection_detail", pk=pk)


@staff_required
@require_GET
def connection_oauth_callback(request):
    return finish_sign_in(request)


@staff_required
@require_POST
def connection_oauth_paste(request, pk):
    """Finish a loopback sign-in from the address staff pasted back."""
    connection = managed_connection_or_404(request, pk)
    cls = connection.connector_class
    if cls is None or not getattr(cls.auth, "loopback_redirect", ""):
        raise Http404
    try:
        params = oauth.pasted_params(cls, request.POST.get("address", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("connection_detail", pk=pk)
    return finish_sign_in(request, params)


def finish_sign_in(request, params=None):
    pending = request.session.get(oauth.PENDING_KEY) or {}
    connection = visible_connections(request.user).filter(pk=pending.get("connection")).first()
    if connection is None or not can_manage(request.user, connection):
        request.session.pop(oauth.PENDING_KEY, None)
        messages.error(request, "This sign-in link expired or did not come from this CRM.")
        return redirect("connections")
    try:
        connection = oauth.finish(request, params)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("connection_detail", pk=connection.pk)
    except ConnectorError as exc:
        connection.status = Connection.Status.NEEDS_REAUTH
        connection.last_error = str(exc)[:500]
        connection.save(update_fields=["status", "last_error", "updated_at"])
        messages.error(request, str(exc))
        return redirect("connection_detail", pk=connection.pk)
    record_audit(request.user, "connection.signed_in", connection)
    if check_connection(connection):
        messages.success(request, f"Connected to {connection.connector_class.name}.")
        if connection.connector_class.usage_label:
            messages.info(
                request,
                f"{connection.connector_class.usage_label}: AI requests you send through this "
                "connection count toward your own plan's usage.",
            )
    else:
        messages.error(request, "Signed in, but the connection test failed.")
    return redirect("connection_detail", pk=connection.pk)
