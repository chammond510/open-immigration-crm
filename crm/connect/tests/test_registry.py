import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    CredentialField,
    DataFlows,
    Maturity,
    OAuthAuth,
    SettingField,
    WebhookSpec,
)
from crm.connect.checks import connector_load_check
from crm.connect.registry import (
    ConnectorRegistry,
    manifest_problems,
    read_catalog_file,
    registry,
)


def make_connector(**overrides):
    attrs = {
        "key": "fictional_phone",
        "name": "Fictional Phone",
        "description": "A fictional phone system.",
        "maturity": Maturity.BETA,
        "capabilities": frozenset({Capability.PHONE}),
        "auth": ApiKeyAuth(fields=(CredentialField("api_key", "API key"),)),
        "data_flows": DataFlows(version=1, receives=("Fictional call records",)),
        **overrides,
    }
    return type("FictionalConnector", (Connector,), attrs)


def write_catalog(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(text)
    handle.close()
    return Path(handle.name)


class ManifestTests(SimpleTestCase):
    def test_complete_manifest_has_no_problems(self):
        self.assertEqual(manifest_problems(make_connector()), [])

    def test_each_missing_or_invalid_field_is_reported(self):
        problems = manifest_problems(
            make_connector(
                key="Bad Key",
                name="",
                description="",
                maturity=Maturity.READY,
                capabilities=frozenset({"telepathy"}),
                auth=None,
                connection_scope="team",
                data_flows=DataFlows(version=0),
                outbound_actions={"send_fax": "fax"},
            )
        )
        joined = " | ".join(problems)
        for expected in (
            "key must be",
            "name is required",
            "description is required",
            "named maintainer",
            "unknown capabilities: telepathy",
            "auth must be",
            "connection_scope",
            "data_flows",
            "undeclared capabilities: fax",
        ):
            self.assertIn(expected, joined)
        self.assertIn(
            "at least one capability",
            " ".join(manifest_problems(make_connector(capabilities=frozenset()))),
        )
        self.assertIn(
            "wanted entries", " ".join(manifest_problems(make_connector(maturity=Maturity.WANTED)))
        )

    def test_webhook_secrets_and_registration_are_checked(self):
        problems = " ".join(
            manifest_problems(
                make_connector(
                    webhooks=WebhookSpec(
                        capabilities=(Capability.PHONE,),
                        secret_source="credential",
                        secret_credential="password",
                        register_label="Set it up for me.",
                    )
                )
            )
        )
        self.assertIn("name one of the credential fields", problems)
        self.assertIn("must implement register_webhook()", problems)
        spec = WebhookSpec(
            capabilities=(Capability.PHONE,),
            secret_source="credential",
            secret_credential="api_key",
        )
        self.assertEqual(manifest_problems(make_connector(webhooks=spec)), [])

    def test_oauth_urls_name_only_app_fields(self):
        def oauth(app_fields=()):
            return OAuthAuth(
                authorize_url="https://login.example.test/{tenant}/authorize",
                token_url="https://login.example.test/{tenant}/token",
                scopes={Capability.PHONE: ("calls",)},
                app_fields=app_fields,
            )

        problems = " ".join(manifest_problems(make_connector(auth=oauth())))
        self.assertIn("OAuth URLs may name only", problems)
        tenant = (SettingField("tenant", "Tenant"),)
        self.assertEqual(manifest_problems(make_connector(auth=oauth(tenant))), [])

    def test_settings_fields_are_checked(self):
        good = (
            SettingField("region", "Region", choices=(("us", "US"), ("eu", "EU")), default="us"),
            SettingField("account", "Account"),
        )
        self.assertEqual(manifest_problems(make_connector(settings_fields=good)), [])
        for fields, expected in (
            ((SettingField("Region", "Region"),), "unique lowercase names"),
            ((SettingField("a1", "A"), SettingField("a1", "B")), "unique lowercase names"),
            ((SettingField("region", "Region", choices=(("us", "US"),)),), "one of its choices"),
        ):
            problems = " ".join(manifest_problems(make_connector(settings_fields=fields)))
            self.assertIn(expected, problems)


class RegistryTests(SimpleTestCase):
    def test_register_validates_and_rejects_duplicates(self):
        fresh = ConnectorRegistry()
        cls = make_connector()
        self.assertIs(fresh.register(cls), cls)
        self.assertIs(fresh.register(cls), cls)
        self.assertIs(fresh.get("fictional_phone"), cls)
        with self.assertRaises(ImproperlyConfigured):
            fresh.register(make_connector())
        with self.assertRaises(ImproperlyConfigured):
            fresh.register(make_connector(key="other", name=""))
        with self.assertRaises(ImproperlyConfigured):
            fresh.register(object)

    def test_hidden_connectors_are_filtered_and_sorted(self):
        fresh = ConnectorRegistry()
        fresh.register(make_connector(key="zeta", name="Zeta"))
        fresh.register(make_connector(key="alpha", name="alpha"))
        fresh.register(make_connector(key="secret", name="Secret", hidden=True))
        self.assertEqual([cls.key for cls in fresh.connectors()], ["alpha", "zeta"])
        self.assertEqual(len(fresh.connectors(include_hidden=True)), 3)

    def test_catalog_merges_installed_and_wanted_entries(self):
        fresh = ConnectorRegistry()
        fresh.register(make_connector(key="installed_phone", name="Installed Phone"))
        fresh.register(make_connector(key="hidden_one", name="Hidden", hidden=True))
        path = write_catalog(
            """
[[connector]]
key = "installed_phone"
name = "Duplicate of an installed connector"
capabilities = ["phone"]

[[connector]]
key = "hidden_one"
name = "Should stay hidden"
capabilities = ["phone"]

[[connector]]
key = "wanted_mail"
name = "Wanted Mail"
capabilities = ["mailbox", "calendar"]
description = "A fictional mail service."
"""
        )
        self.addCleanup(path.unlink)
        with override_settings(CONNECTOR_CATALOG=path):
            catalog = fresh.catalog()
        self.assertEqual([entry.key for entry in catalog], ["installed_phone", "wanted_mail"])
        self.assertEqual(catalog[0].maturity, Maturity.BETA)
        self.assertIsNotNone(catalog[0].connector)
        self.assertEqual(catalog[1].maturity, Maturity.WANTED)
        self.assertEqual(catalog[1].capabilities, ("calendar", "mailbox"))

    def test_catalog_file_validation(self):
        self.assertEqual(read_catalog_file(None), [])
        self.assertEqual(read_catalog_file("/nonexistent/catalog.toml"), [])
        bad_key = write_catalog(
            '[[connector]]\nkey = "Bad Key"\nname = "X"\ncapabilities = ["phone"]\n'
        )
        self.addCleanup(bad_key.unlink)
        with self.assertRaises(ImproperlyConfigured):
            read_catalog_file(bad_key)
        bad_capability = write_catalog(
            '[[connector]]\nkey = "x_y"\nname = "X"\ncapabilities = ["fax"]\n'
        )
        self.addCleanup(bad_capability.unlink)
        with self.assertRaises(ImproperlyConfigured):
            read_catalog_file(bad_capability)

    def test_shipped_catalog_is_valid_and_hides_the_example(self):
        entries = read_catalog_file(settings.CONNECTOR_CATALOG)
        self.assertGreater(len(entries), 50)
        self.assertEqual(len({entry.key for entry in entries}), len(entries))
        self.assertFalse(any(entry.key == "example" for entry in registry.catalog()))
        self.assertIsNotNone(registry.get("example"))
        self.assertEqual(registry.load_errors, [])

    def test_load_records_errors_and_runs_once(self):
        good = make_connector(key="from_entry_point", name="From Entry Point")
        good_entry = mock.Mock()
        good_entry.load.return_value = good
        broken_entry = mock.Mock()
        broken_entry.name = "broken"
        broken_entry.load.side_effect = ImportError("missing dependency")
        fresh = ConnectorRegistry()
        with (
            override_settings(CONNECTOR_PACKAGES=["connectors", "no_such_connector_package"]),
            mock.patch(
                "crm.connect.registry.entry_points", return_value=[good_entry, broken_entry]
            ) as patched,
        ):
            fresh.load()
            fresh.load()
        self.assertEqual(patched.call_count, 1)
        self.assertIs(fresh.get("from_entry_point"), good)
        joined = " | ".join(fresh.load_errors)
        self.assertIn("no_such_connector_package", joined)
        self.assertIn("broken: ImportError: missing dependency", joined)

    def test_load_errors_surface_as_system_check_warnings(self):
        with mock.patch.object(registry, "load_errors", ["fictional: ImportError: boom"]):
            warnings = connector_load_check(None)
        self.assertEqual([warning.id for warning in warnings], ["connect.W001"])
        self.assertIn("fictional", warnings[0].msg)
