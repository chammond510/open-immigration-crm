from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from crm.ai import memos, suggestions
from crm.comms.models import Communication
from crm.comms.sharing import shareable, withheld_reason
from crm.connect.models import Connection
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.mcp import tools
from crm.models import AuditLog, WorkItem
from crm.tests.factories import make_matter
from crm.translations import services
from crm.translations.models import Translation
from crm.translations.tests.test_translations import TranslationCase

TERMS = "Fictional terms limit sharing these messages with other services."


class SharingLimitTestCase(TranslationCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(registry.get("example"), "sharing_limits", TERMS))
        self.limited = self.texting("example", "Client messages")
        self.open = self.texting("example_messaging", "Front desk texts")
        self.matter = make_matter(self.contact, self.admin)
        self.kept_out = self.logged(self.limited, "Mi pasaporte venció.")
        self.shared = self.logged(self.open, "Llego a las diez.")
        # The message in TranslationCase was logged by hand, with no connection.

    def texting(self, key, label):
        connection = create_connection(
            key,
            label=label,
            created_by=self.admin,
            capabilities=["texting"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(connection, self.admin)
        return connection

    def logged(self, connection, summary):
        return Communication.objects.create(
            channel=Communication.Channel.TEXT,
            direction=Communication.Direction.INBOUND,
            summary=summary,
            contact=self.contact,
            matter=self.matter,
            source_connection=connection,
            external_id=f"in-{Communication.objects.count()}",
        )

    def summaries(self, items):
        return {item["summary"] for item in items}


class SharingLimitTests(SharingLimitTestCase):
    def test_withheld_messages_are_left_out_of_ai_features(self):
        everyone = {self.kept_out.summary, self.shared.summary, self.message.summary}
        shared = everyone - {self.kept_out.summary}
        self.assertEqual({c.summary for c in shareable(Communication.objects.all())}, shared)
        self.assertEqual(withheld_reason(self.kept_out), TERMS)
        self.assertEqual(withheld_reason(self.shared), "")
        # The MCP server's tools, which the matter assistant uses too.
        ctx = SimpleNamespace(request=RequestFactory().get("/"), user=self.staff)
        self.assertEqual(
            self.summaries(tools.list_communications(ctx, {})["communications"]), shared
        )
        contact = tools.get_contact(ctx, {"contact_id": str(self.contact.pk)})
        self.assertEqual(self.summaries(contact["recent_communications"]), shared)
        matter = tools.get_matter(ctx, {"matter_id": str(self.matter.pk)})
        self.assertEqual(
            self.summaries(matter["recent_communications"]), shared - {self.message.summary}
        )
        # Task suggestions and consult prep memos.
        found, _ = suggestions.new_communications(
            timezone.now() - timedelta(hours=1), timezone.now()
        )
        self.assertEqual({c.summary for c in found}, shared)
        consult = WorkItem.objects.create(
            kind=WorkItem.Kind.APPOINTMENT,
            title="Consultation",
            contact=self.contact,
            scheduled_for=timezone.now() + timedelta(hours=2),
        )
        records = memos.memo_records(consult)
        self.assertEqual(self.summaries(records["recent_communications"]), shared)
        # Once a superuser allows it, the connection's messages are included.
        Connection.objects.filter(pk=self.limited.pk).update(ai_use_allowed=True)
        self.assertEqual(
            self.summaries(tools.list_communications(ctx, {})["communications"]), everyone
        )
        self.assertEqual(withheld_reason(self.kept_out), "")

    def test_withheld_messages_are_not_translated(self):
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, f"Not offered for translation. {TERMS}", count=1)
        self.assertContains(page, "<summary>Translate</summary>", count=2)
        answer = self.translate(self.kept_out, f"service:{self.service.pk}")
        self.assertContains(
            answer,
            f"Not translated: {TERMS} Someone who manages connections and AI can allow it on "
            "the connection.",
        )
        self.assertFalse(Translation.objects.exists())
        with self.assertRaisesMessage(services.TranslationUnavailable, TERMS):
            services.translate_communication(self.kept_out, self.service, "en", actor=self.staff)

    def test_a_superuser_decides_on_the_connection_page(self):
        url = reverse("connection_detail", args=[self.limited.pk])
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertContains(page, "AI and translation")
        self.assertContains(page, "kept out of AI features, the MCP server, and translation.")
        self.assertContains(page, TERMS)
        self.assertContains(page, "Check the provider's terms with the firm's lawyer")
        allow = reverse("connection_ai_use", args=[self.limited.pk])
        response = self.client.post(allow, {"allowed": "yes"}, follow=True)
        self.assertContains(response, "may now read this connection&#x27;s messages.")
        self.assertContains(response, "Stop AI and translation access")
        self.limited.refresh_from_db()
        self.assertTrue(self.limited.ai_use_allowed)
        self.client.post(allow, {"allowed": "no"})
        self.limited.refresh_from_db()
        self.assertFalse(self.limited.ai_use_allowed)
        self.assertEqual(
            list(
                AuditLog.objects.filter(action__startswith="connection.ai_use").values_list(
                    "action", flat=True
                )
            ),
            ["connection.ai_use_withheld", "connection.ai_use_allowed"],
        )
        # Connectors without limits have no such setting.
        self.assertNotContains(
            self.client.get(reverse("connection_detail", args=[self.open.pk])),
            "AI and translation",
        )
        open_url = reverse("connection_ai_use", args=[self.open.pk])
        self.assertEqual(self.client.post(open_url, {"allowed": "yes"}).status_code, 404)
        # Only superusers decide.
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(allow, {"allowed": "yes"}).status_code, 403)
