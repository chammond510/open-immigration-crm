from contextlib import contextmanager
from unittest import mock

from django.test import TestCase
from django_tasks_db.models import DBTaskResult

from crm.ai.models import AISettings
from crm.connect.models import Connection
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.tests.factories import make_user


def queued(task):
    return DBTaskResult.objects.filter(task_path=task.module_path).count()


@contextmanager
def scripted(*answers):
    """Make the example model give these answers in turn; yield the payloads it was sent."""
    remaining = iter(answers)
    sent = []

    def perform(connector, action, payload):
        sent.append(payload)
        return {
            "id": "scripted",
            "model": "scripted-model",
            "text": "",
            "tool_calls": [],
            "data": None,
            "input_tokens": 10,
            "output_tokens": 5,
            "stop_reason": "end_turn",
            "raw": None,
            **next(remaining),
        }

    with mock.patch.object(registry.get("example_ai"), "perform", perform):
        yield sent


class AITestCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.firm = self.connect("Firm model")
        self.mine = self.connect("Paralegal's model", owner=self.staff)

    def connect(self, label, owner=None):
        connection = create_connection(
            "example_ai",
            label=label,
            created_by=self.admin,
            capabilities=["ai_models"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(connection, self.admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False, owner=owner)
        connection.refresh_from_db()
        return connection

    def turn_on(self, *keys, background=None):
        ai_settings = AISettings.load()
        ai_settings.enabled = True
        ai_settings.features = {key: True for key in keys}
        ai_settings.background_connection = background
        ai_settings.save()
