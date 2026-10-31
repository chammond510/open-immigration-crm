import sys
import tempfile
from importlib.metadata import EntryPoint
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from crm.connect import registry as registry_module
from crm.connect.base import API_VERSION
from crm.connect.registry import ENTRY_POINT_GROUP, ConnectorRegistry, api_version_problem

from .test_registry import make_connector


class ApiVersionTests(SimpleTestCase):
    def test_versions_an_installation_can_run(self):
        major, minor = API_VERSION
        self.assertEqual(api_version_problem((major, minor)), "")
        if minor:
            self.assertEqual(api_version_problem((major, minor - 1)), "")
        for newer in ((major, minor + 1), (major + 1, 0), (major - 1, 0)):
            with self.subTest(version=newer):
                self.assertIn("this installation has", api_version_problem(newer))
        for broken in ("1.0", (1,), (1, "0"), [1, 0]):
            with self.subTest(version=broken):
                self.assertIn("two whole numbers", api_version_problem(broken))
        problems = registry_module.manifest_problems(make_connector(api_version=(99, 0)))
        self.assertEqual(len(problems), 1)


class StartConnectorTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def start(self, key="fictional_shipper", name="Fictional Shipper", capability="shipping"):
        out = StringIO()
        call_command(
            "startconnector",
            key,
            name=name,
            capability=capability,
            directory=str(self.directory),
            stdout=out,
        )
        return self.directory / f"open-immigration-crm-{key.replace('_', '-')}", out.getvalue()

    def test_a_package_that_installs_on_its_own(self):
        package, out = self.start()
        self.assertIn("python manage.py test oicrm_fictional_shipper", out)
        pyproject = (package / "pyproject.toml").read_text()
        self.assertIn('[project.entry-points."open_immigration_crm.connectors"]', pyproject)
        self.assertIn(
            'fictional_shipper = "oicrm_fictional_shipper:FictionalShipperConnector"', pyproject
        )
        module = (package / "oicrm_fictional_shipper" / "__init__.py").read_text()
        self.assertIn("capabilities = frozenset({Capability.SHIPPING})", module)
        self.assertIn(f"api_version = ({API_VERSION[0]}, {API_VERSION[1]})", module)
        test = package / "oicrm_fictional_shipper" / "test_fictional_shipper.py"
        for path in (package / "oicrm_fictional_shipper" / "__init__.py", test):
            compile(path.read_text(), str(path), "exec")
        self.assertIn("shipping capability", (package / "README.md").read_text())

    def test_what_it_refuses(self):
        cases = [
            ({"key": "Bad Key"}, "lowercase letters"),
            ({"name": 'Quote " name'}, "Keep the name"),
            ({"key": "google_drive"}, "already installed"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesMessage(CommandError, message):
                self.start(**overrides)
        self.start()
        with self.assertRaisesMessage(CommandError, "already exists"):
            self.start()
        with self.assertRaises(CommandError):
            self.start(key="fictional_hooks", capability="automation")

    @override_settings(CONNECTOR_PACKAGES=[])
    def test_the_registry_loads_a_package_through_its_entry_point(self):
        package, _ = self.start(key="fictional_entry", name="Fictional Entry")
        sys.path.insert(0, str(package))
        self.addCleanup(sys.path.remove, str(package))
        self.addCleanup(sys.modules.pop, "oicrm_fictional_entry", None)
        # The package registers itself on import too; keep the shared registry clean.
        self.addCleanup(registry_module.registry._connectors.pop, "fictional_entry", None)
        point = EntryPoint(
            "fictional_entry", "oicrm_fictional_entry:FictionalEntryConnector", ENTRY_POINT_GROUP
        )
        fresh = ConnectorRegistry()
        with mock.patch.object(registry_module, "entry_points", return_value=[point]):
            fresh.load()
        self.assertEqual(fresh.load_errors, [])
        self.assertEqual(fresh.get("fictional_entry").name, "Fictional Entry")

    @override_settings(CONNECTOR_PACKAGES=[])
    def test_a_package_for_a_newer_interface_is_refused_with_a_reason(self):
        package, _ = self.start(key="fictional_newer", name="Fictional Newer")
        module = package / "oicrm_fictional_newer" / "__init__.py"
        module.write_text(
            module.read_text().replace(
                f"api_version = ({API_VERSION[0]}, {API_VERSION[1]})",
                f"api_version = ({API_VERSION[0] + 1}, 0)",
            )
        )
        sys.path.insert(0, str(package))
        self.addCleanup(sys.path.remove, str(package))
        self.addCleanup(sys.modules.pop, "oicrm_fictional_newer", None)
        point = EntryPoint(
            "fictional_newer", "oicrm_fictional_newer:FictionalNewerConnector", ENTRY_POINT_GROUP
        )
        fresh = ConnectorRegistry()
        with mock.patch.object(registry_module, "entry_points", return_value=[point]):
            fresh.load()
        self.assertIsNone(fresh.get("fictional_newer"))
        [error] = fresh.load_errors
        self.assertIn("written for connector API", error)
