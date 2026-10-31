"""Discovers connectors and merges them with the catalog of wanted providers."""

import importlib
import pkgutil
import re
import tomllib
from dataclasses import dataclass
from importlib.metadata import entry_points

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .base import (
    API_VERSION,
    AUTH_TYPES,
    WEBHOOK_SECRET_SOURCES,
    ApiKeyAuth,
    Capability,
    ConnectionScope,
    Connector,
    KeyExchangeAuth,
    Maturity,
    OAuthAuth,
)

ENTRY_POINT_GROUP = "open_immigration_crm.connectors"
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    capabilities: tuple[str, ...]
    maturity: str
    description: str = ""
    connector: type[Connector] | None = None
    # For a wanted provider: the key of an installed connector it already works through.
    works_through: str = ""
    # For a wanted provider: its developer documentation, and how its API is opened up.
    docs_url: str = ""
    api_access: str = ""


# How a wanted provider's API is opened up to a connector's author.
API_ACCESS = {
    "open": "Anyone with an account can get credentials.",
    "approval": "The provider approves an app before it can be used with firms' accounts.",
    "partner": "Only for the provider's partners.",
    "none": "No public API.",
}


def api_version_problem(version):
    """Why this installation cannot run a connector written for ``version``, or ""."""
    if (
        not isinstance(version, tuple)
        or len(version) != 2
        or not all(isinstance(part, int) for part in version)
    ):
        return "api_version must be two whole numbers, such as (1, 0)"
    wanted, have = ".".join(map(str, version)), ".".join(map(str, API_VERSION))
    if version[0] != API_VERSION[0] or version[1] > API_VERSION[1]:
        return f"it was written for connector API {wanted}, and this installation has {have}"
    return ""


def manifest_problems(cls):
    """List what is missing or invalid in a connector's declared manifest."""
    problems = []
    version_problem = api_version_problem(cls.api_version)
    if version_problem:
        problems.append(version_problem)
    if not KEY_PATTERN.match(cls.key or ""):
        problems.append("key must be 2-41 lowercase letters, digits, or underscores")
    if not cls.name:
        problems.append("name is required")
    if not cls.description:
        problems.append("description is required")
    if cls.maturity not in (Maturity.READY, Maturity.BETA):
        problems.append("maturity must be ready or beta; wanted entries belong in the catalog")
    if cls.maturity == Maturity.READY and not cls.maintainers:
        problems.append("ready connectors need at least one named maintainer")
    if not cls.capabilities:
        problems.append("at least one capability is required")
    unknown = set(cls.capabilities) - set(Capability.values)
    if unknown:
        problems.append(f"unknown capabilities: {', '.join(sorted(unknown))}")
    if not isinstance(cls.auth, AUTH_TYPES):
        problems.append("auth must be ApiKeyAuth, OAuthAuth, KeyExchangeAuth, or PublicDataAuth")
    if isinstance(cls.auth, KeyExchangeAuth) and (
        cls.authorize_query is Connector.authorize_query
        or cls.exchange_code is Connector.exchange_code
    ):
        problems.append("key-exchange sign-in needs authorize_query() and exchange_code()")
    if cls.connection_scope not in ConnectionScope.values:
        problems.append("connection_scope must be firm or user")
    names = [setting.name for setting in cls.settings_fields]
    if len(set(names)) != len(names) or not all(KEY_PATTERN.match(name) for name in names):
        problems.append("settings_fields need unique lowercase names")
    if any(
        setting.choices and setting.default not in dict(setting.choices)
        for setting in cls.settings_fields
    ):
        problems.append("a setting's default must be one of its choices")
    flows = cls.data_flows
    if flows is None or flows.version < 1 or not (flows.sends or flows.receives):
        problems.append("data_flows must have a version of at least 1 and list what moves")
    if not set(cls.sync_capabilities) <= set(cls.capabilities):
        problems.append("sync_capabilities must be declared capabilities")
    if cls.webhooks is not None and (
        not cls.webhooks.capabilities
        or not set(cls.webhooks.capabilities) <= set(cls.capabilities)
        or cls.webhooks.secret_source not in WEBHOOK_SECRET_SOURCES
    ):
        problems.append("webhooks must use declared capabilities and a known secret source")
    if isinstance(cls.auth, OAuthAuth):
        named = set(re.findall(r"{(\w+)}", cls.auth.authorize_url + cls.auth.token_url))
        if not named <= {f.name for f in cls.auth.app_fields}:
            problems.append("OAuth URLs may name only the auth's app_fields")
        if cls.auth.token_auth not in ("post", "basic"):
            problems.append('OAuth token_auth must be "post" or "basic"')
    if cls.webhooks is not None and cls.webhooks.secret_source == "credential":
        names = {f.name for f in cls.auth.fields} if isinstance(cls.auth, ApiKeyAuth) else set()
        if cls.webhooks.secret_credential not in names:
            problems.append("a credential webhook secret must name one of the credential fields")
    if (
        cls.webhooks is not None
        and cls.webhooks.register_label
        and cls.register_webhook is Connector.register_webhook
    ):
        problems.append("webhooks that offer registration must implement register_webhook()")
    if not set(cls.owner_sync_actions) <= set(cls.outbound_actions):
        problems.append("owner_sync_actions must be declared outbound actions")
    stray = set(cls.outbound_actions.values()) - set(cls.capabilities)
    if stray:
        problems.append(f"outbound actions use undeclared capabilities: {', '.join(sorted(stray))}")
    if (
        Capability.AUTOMATION in cls.capabilities
        and cls.outbound_actions.get("deliver_event") != Capability.AUTOMATION
    ):
        problems.append("automation connectors must declare the deliver_event outbound action")
    if cls.reply_window is not None and (
        not cls.reply_window
        or cls.outbound_actions.get("send_template") != Capability.TEXTING
        or cls.message_templates is Connector.message_templates
    ):
        problems.append(
            "a reply window needs a send_template texting action and message_templates()"
        )
    return problems


class ConnectorRegistry:
    def __init__(self):
        self._connectors = {}
        self.load_errors = []
        self._loaded = False

    def register(self, cls):
        """Class decorator that validates and records a connector."""
        if not (isinstance(cls, type) and issubclass(cls, Connector)):
            raise ImproperlyConfigured(f"{cls!r} is not a Connector subclass.")
        problems = manifest_problems(cls)
        if problems:
            raise ImproperlyConfigured(f"Connector {cls.__name__}: {'; '.join(problems)}.")
        existing = self._connectors.get(cls.key)
        if existing is not None and existing is not cls:
            raise ImproperlyConfigured(f"Two connectors use the key {cls.key!r}.")
        self._connectors[cls.key] = cls
        return cls

    @staticmethod
    def offered(cls):
        """Whether a connector is offered: always, or while its Beta flag is on."""
        return not cls.flag or cls.flag in settings.CONNECTOR_FLAGS

    def get(self, key):
        cls = self._connectors.get(key)
        return cls if cls is not None and self.offered(cls) else None

    def connectors(self, *, include_hidden=False):
        return sorted(
            (
                cls
                for cls in self._connectors.values()
                if (include_hidden or not cls.hidden) and self.offered(cls)
            ),
            key=lambda cls: cls.name.lower(),
        )

    def load(self):
        """Import first-party connector packages and installed entry points once."""
        if self._loaded:
            return
        self._loaded = True
        for package_name in getattr(settings, "CONNECTOR_PACKAGES", []):
            try:
                package = importlib.import_module(package_name)
            except Exception as exc:
                self.load_errors.append(f"{package_name}: {exc.__class__.__name__}: {exc}")
                continue
            for module in pkgutil.iter_modules(package.__path__):
                self._import(f"{package_name}.{module.name}")
        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded = entry_point.load()
                if isinstance(loaded, type) and issubclass(loaded, Connector):
                    self.register(loaded)
            except Exception as exc:
                self.load_errors.append(f"{entry_point.name}: {exc.__class__.__name__}: {exc}")

    def _import(self, module_name):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            self.load_errors.append(f"{module_name}: {exc.__class__.__name__}: {exc}")

    def catalog(self, *, include_hidden=False):
        """Installed connectors plus wanted providers from the catalog file, by name."""
        entries = {
            cls.key: CatalogEntry(
                key=cls.key,
                name=cls.name,
                capabilities=tuple(sorted(cls.capabilities)),
                maturity=cls.maturity,
                description=cls.description,
                connector=cls,
            )
            for cls in self.connectors(include_hidden=include_hidden)
        }
        for wanted in read_catalog_file(getattr(settings, "CONNECTOR_CATALOG", None)):
            if wanted.key not in entries and self.get(wanted.key) is None:
                entries[wanted.key] = wanted
        return sorted(entries.values(), key=lambda entry: entry.name.lower())


def read_catalog_file(path):
    """Read wanted providers from a TOML file of [[connector]] tables."""
    if not path:
        return []
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return []
    entries = []
    for item in data.get("connector", []):
        key = item.get("key", "")
        capabilities = tuple(item.get("capabilities", ()))
        if not KEY_PATTERN.match(key) or not item.get("name"):
            raise ImproperlyConfigured(f"Catalog entry {item!r} needs a valid key and a name.")
        if item.get("works_through") and not KEY_PATTERN.match(item["works_through"]):
            raise ImproperlyConfigured(f"Catalog entry {key!r} has an invalid works_through key.")
        if item.get("docs_url") and not str(item["docs_url"]).startswith("https://"):
            raise ImproperlyConfigured(f"Catalog entry {key!r} needs an https:// docs_url.")
        if item.get("api_access", "") not in ("", *API_ACCESS):
            raise ImproperlyConfigured(
                f"Catalog entry {key!r} has api_access other than {', '.join(API_ACCESS)}."
            )
        unknown = set(capabilities) - set(Capability.values)
        if not capabilities or unknown:
            raise ImproperlyConfigured(
                f"Catalog entry {key!r} has missing or unknown capabilities."
            )
        entries.append(
            CatalogEntry(
                key=key,
                name=item["name"],
                capabilities=tuple(sorted(capabilities)),
                maturity=Maturity.WANTED,
                description=item.get("description", ""),
                works_through=item.get("works_through", ""),
                docs_url=item.get("docs_url", ""),
                api_access=item.get("api_access", ""),
            )
        )
    return entries


registry = ConnectorRegistry()
register = registry.register
