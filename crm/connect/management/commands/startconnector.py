"""Write a skeleton package for a connector built outside this repository.

The package installs on its own and registers through the
``open_immigration_crm.connectors`` entry point, with a connector whose manifest is
complete and a contract test that passes against a fake provider, so a contributor
starts from something that loads and tests cleanly. See docs/CONNECTOR_SDK.md.
"""

import re
from pathlib import Path
from string import Template

from django.core.management.base import BaseCommand, CommandError

from crm.connect.base import API_VERSION, Capability
from crm.connect.registry import KEY_PATTERN, registry

SKELETON = Path(__file__).resolve().parents[2] / "skeleton"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'()+-]{1,59}")
# The contract tests for these need more than a skeleton gives them.
NOT_OFFERED = {
    Capability.AUTOMATION: "automation is built in; see connectors/webhooks",
    Capability.SIGN_IN: "start from connectors/oidc, whose tests prove a sign-in",
}


class Command(BaseCommand):
    help = "Write a skeleton package for a connector built outside this repository."

    def add_arguments(self, parser):
        parser.add_argument("key", help="Lowercase letters, digits, and underscores.")
        parser.add_argument("--name", required=True, help="The provider's name.")
        parser.add_argument(
            "--capability",
            required=True,
            choices=[value for value in Capability.values if value not in NOT_OFFERED],
        )
        parser.add_argument("--directory", default=".", help="Where to write the package.")

    def handle(self, *args, key, name, capability, directory, **options):
        if not KEY_PATTERN.match(key):
            raise CommandError("The key must be 2-41 lowercase letters, digits, or underscores.")
        if not NAME.fullmatch(name):
            raise CommandError(
                "Keep the name to letters, digits, spaces, and . & ' ( ) + -, up to 60."
            )
        registry.load()
        if registry.get(key) is not None:
            raise CommandError(f"A connector with the key {key!r} is already installed.")
        values = {
            "key": key,
            "name": name,
            "module": f"oicrm_{key}",
            "dist": f"open-immigration-crm-{key.replace('_', '-')}",
            "class_name": "".join(part.title() for part in key.split("_")) + "Connector",
            "capability": Capability(capability).name,
            "capability_label": Capability(capability).label.lower(),
            "api_major": API_VERSION[0],
            "api_minor": API_VERSION[1],
        }
        target = Path(directory).expanduser() / values["dist"]
        if target.exists():
            raise CommandError(f"{target} already exists.")
        files = {
            "pyproject.toml": "pyproject.toml.tmpl",
            "README.md": "README.md.tmpl",
            f"{values['module']}/__init__.py": "module/__init__.py.tmpl",
            f"{values['module']}/test_{key}.py": "module/test_module.py.tmpl",
        }
        for written, source in files.items():
            path = target / written
            path.parent.mkdir(parents=True, exist_ok=True)
            text = Template((SKELETON / source).read_text()).substitute(values)
            path.write_text(text)
        self.stdout.write(f"Wrote {target}")
        self.stdout.write(
            f"Next: pip install -e {target}, then python manage.py test {values['module']}"
        )
