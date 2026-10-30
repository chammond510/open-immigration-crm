from django.test import TestCase
from django.urls import reverse

from crm.ai import features
from crm.ai.client import AIUnavailable, Message, Tool, ai_connections, generate
from crm.ai.models import AIRequest, AISettings
from crm.connect.models import Connection, OutboundRequest
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.models import AuditLog
from crm.tests.factories import make_user

DRAFTS = features.register(
    features.Feature("fictional_drafts", "Fictional drafts", "Drafts fictional text.")
)
MEMOS = features.register(
    features.Feature("fictional_memos", "Fictional memos", "Prepares memos.", background=True)
)


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


class GenerateTests(AITestCase):
    def ask(self, feature=DRAFTS.key, **kwargs):
        return generate(feature=feature, messages=[Message("user", "Hello there")], **kwargs)

    def test_everything_is_off_until_turned_on(self):
        self.assertFalse(AISettings.load().enabled)
        with self.assertRaisesMessage(AIUnavailable, "This AI feature is off"):
            self.ask(actor=self.staff, connection=self.firm)
        self.turn_on(MEMOS.key)
        with self.assertRaisesMessage(AIUnavailable, "This AI feature is off"):
            self.ask(actor=self.staff, connection=self.firm)
        self.assertFalse(OutboundRequest.objects.exists())

    def test_a_staff_members_request_and_its_log(self):
        self.turn_on(DRAFTS.key)
        reply = self.ask(actor=self.staff, connection=self.mine)
        self.assertEqual(reply.text, "ereht olleH")
        self.assertEqual(reply.model, "example-model-1")
        logged = AIRequest.objects.get()
        self.assertEqual(
            (logged.feature, logged.connection, logged.actor, logged.status, logged.is_background),
            ("fictional_drafts", self.mine, self.staff, "sent", False),
        )
        self.assertGreater(logged.input_tokens, 0)
        self.assertEqual(OutboundRequest.objects.get().summary, "AI: Fictional drafts")
        self.assertNotIn("Hello", str(logged.__dict__))

    def test_connections_people_may_use(self):
        self.turn_on(DRAFTS.key)
        other = make_user("other", superuser=False)
        self.assertEqual({c.label for c in ai_connections(other)}, {"Firm model"})
        with self.assertRaisesMessage(AIUnavailable, "Choose an AI connection you may use"):
            self.ask(actor=other, connection=self.mine)
        self.ask(actor=other, connection=self.firm)

    def test_background_work_uses_only_the_firm_connection(self):
        self.turn_on(MEMOS.key)
        with self.assertRaisesMessage(AIUnavailable, "Choose the connection for background work"):
            self.ask(feature=MEMOS.key)
        self.turn_on(MEMOS.key, background=self.mine)
        with self.assertRaisesMessage(AIUnavailable, "Choose the connection for background work"):
            self.ask(feature=MEMOS.key)
        self.turn_on(MEMOS.key, background=self.firm)
        self.ask(feature=MEMOS.key)
        logged = AIRequest.objects.get()
        self.assertTrue(logged.is_background)
        self.assertIsNone(logged.actor)
        self.assertTrue(OutboundRequest.objects.get().is_automation)

    def test_tools_schemas_and_the_providers_copy(self):
        self.turn_on(DRAFTS.key)
        reply = generate(
            feature=DRAFTS.key,
            messages=[Message("user", "Please use a tool")],
            tools=[Tool("find_matter", "Find a matter.")],
            schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            actor=self.staff,
            connection=self.firm,
        )
        self.assertEqual(reply.tool_calls[0].name, "find_matter")
        self.assertEqual(reply.data, {"summary": "fictional"})
        followup = reply.as_message()
        self.assertEqual(
            (followup.role, followup.raw), ("assistant", {"echo": "Please use a tool"})
        )

    def test_preview_mode_and_refusals_are_logged(self):
        self.turn_on(DRAFTS.key)
        Connection.objects.filter(pk=self.firm.pk).update(preview_mode=True)
        self.firm.refresh_from_db()
        with self.assertRaisesMessage(AIUnavailable, "is in preview mode, so nothing was sent"):
            self.ask(actor=self.staff, connection=self.firm)
        self.assertEqual(AIRequest.objects.get().status, "previewed")


class SettingsPageTests(AITestCase):
    def test_superusers_turn_features_on_and_test_a_connection(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("ai_settings"))
        self.assertContains(page, "AI features are off, so nothing is sent to an AI model.")
        self.assertContains(page, "Fictional drafts")
        response = self.client.post(
            reverse("ai_settings"),
            {
                "enabled": "on",
                "background_connection": self.firm.pk,
                f"feature_{DRAFTS.key}": "on",
            },
            follow=True,
        )
        self.assertContains(response, "AI features are on.")
        ai_settings = AISettings.load()
        self.assertTrue(ai_settings.feature_on(DRAFTS.key))
        self.assertFalse(ai_settings.feature_on(MEMOS.key))
        self.assertEqual(ai_settings.background_connection, self.firm)
        self.assertTrue(AuditLog.objects.filter(action="ai_settings.updated").exists())
        # A personal connection is never offered for background work.
        refused = self.client.post(reverse("ai_settings"), {"background_connection": self.mine.pk})
        self.assertEqual(refused.status_code, 302)
        self.assertEqual(AISettings.load().background_connection, self.firm)

        tested = self.client.post(
            reverse("ai_connection_test"), {"connection": self.firm.pk}, follow=True
        )
        self.assertContains(tested, "Firm model answered with example-model-1")
        self.assertEqual(AIRequest.objects.get().feature, features.CONNECTION_TEST)
        page = self.client.get(reverse("ai_settings"))
        self.assertContains(page, "<td>Connection test</td>", html=True)

    def test_staff_see_but_cannot_change_settings(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("ai_settings"))
        self.assertContains(page, "A superuser can turn them on.")
        self.assertContains(page, "Paralegal&#x27;s model")
        self.assertEqual(
            self.client.post(reverse("ai_settings"), {"enabled": "on"}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("ai_connection_test"), {"connection": self.mine.pk}
            ).status_code,
            403,
        )
        self.assertFalse(AISettings.load().enabled)
