from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.comms.models import Communication
from crm.comms.texting import can_text_freely, reply_window_ends
from crm.connect.models import Connection, OutboundRequest
from crm.connect.records import TextRecord
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.signals import records_received
from crm.models import MatterParty
from crm.tests.factories import make_contact, make_matter, make_user

NUMBER = "+12025550144"


class ReplyWindowTestCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.connection = self.connect("example_messaging", "Client messages")
        self.contact = make_contact(self.staff, phone="(202) 555-0144")
        self.client.force_login(self.staff)

    def connect(self, key, label, *, api_key="example-fictional", owner=None):
        connection = create_connection(
            key,
            label=label,
            created_by=self.admin,
            capabilities=["texting"],
            credentials={"api_key": api_key},
        )
        acknowledge_data_flows(connection, self.admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False, owner=owner)
        connection.refresh_from_db()
        return connection

    def wrote(self, ago, connection=None, number=NUMBER):
        """The contact's message to the connection, ``ago`` before now."""
        records_received.send(
            sender=None,
            connection=connection or self.connection,
            capability="texting",
            records=[
                TextRecord(
                    external_id=f"in-{Communication.objects.count()}",
                    occurred_at=timezone.now() - ago,
                    direction="inbound",
                    remote=number,
                    body="Hello, it is me.",
                )
            ],
            source="webhook",
        )

    def contact_page(self):
        return self.client.get(reverse("contact_detail", args=[self.contact.pk]))

    def send_text(self, **overrides):
        data = {
            "contact": self.contact.pk,
            "connection": self.connection.pk,
            "body": "Can you come in on Tuesday?",
            **overrides,
        }
        return self.client.post(reverse("communication_send_text"), data, follow=True)

    def template_page(self, **params):
        query = {"contact": self.contact.pk, "connection": self.connection.pk, **params}
        return self.client.get(reverse("communication_send_template"), query)

    def send_template(self, template="reply_request:en", **overrides):
        data = {
            "contact": self.contact.pk,
            "connection": self.connection.pk,
            "template": template,
            "value-name": "Ana",
            **overrides,
        }
        return self.client.post(reverse("communication_send_template"), data, follow=True)


class ReplyWindowTests(ReplyWindowTestCase):
    def test_until_the_contact_writes_only_templates_go(self):
        page = self.contact_page()
        self.assertContains(
            page,
            f"Client messages allows free-form messages only within 24 hours of {self.contact}"
            f"'s last message, and {self.contact} has not written to it.",
        )
        self.assertContains(page, "Send an approved template")
        self.assertNotContains(page, f'action="{reverse("communication_send_text")}"')
        response = self.send_text()
        self.assertContains(response, "Send one of its approved templates instead.")
        self.assertFalse(OutboundRequest.objects.exists())

    def test_a_message_from_the_contact_opens_the_window_for_a_day(self):
        self.wrote(timedelta(hours=1))
        self.assertTrue(can_text_freely(self.connection, self.contact))
        page = self.contact_page()
        self.assertContains(page, "Client messages allows free-form messages until")
        self.assertContains(page, f'action="{reverse("communication_send_text")}"')
        self.assertContains(self.send_text(), "Text sent through Client messages.")
        sent = Communication.objects.get(direction="outbound")
        self.assertEqual(sent.summary, "Can you come in on Tuesday?")
        # A day after the contact's last message, the window has closed.
        Communication.objects.filter(direction="inbound").update(
            occurred_at=timezone.now() - timedelta(hours=25)
        )
        self.assertFalse(can_text_freely(self.connection, self.contact))
        page = self.contact_page()
        self.assertContains(page, "'s last message.</p>")
        self.assertNotContains(page, "has not written to it")
        self.assertContains(self.send_text(), "allows free-form messages only within 24 hours")

    def test_only_the_contacts_messages_to_that_connection_count(self):
        other = self.connect("example", "Front desk texts")
        self.wrote(timedelta(hours=1), connection=other)
        self.wrote(timedelta(hours=1), number="+12025550145")
        self.assertIsNone(reply_window_ends(self.connection, self.contact))
        # A texting service with no reply window always takes free-form texts.
        self.assertTrue(can_text_freely(other, self.contact))
        page = self.contact_page()
        self.assertContains(page, f'name="connection" value="{other.pk}"')
        self.assertContains(page, "Sent through Front desk texts.")
        self.assertContains(page, "has not written to it")


class TemplateTests(ReplyWindowTestCase):
    def test_templates_are_listed_filled_in_and_logged(self):
        page = self.template_page()
        self.assertContains(page, f"Send an approved template to {self.contact}")
        self.assertContains(page, f"{self.contact} has not written to Client messages")
        self.assertContains(
            page, "Hello [Client&#x27;s first name], this is your law firm. Please reply"
        )
        self.assertContains(page, "office_closed")
        self.assertContains(page, "For example: Ana")
        response = self.send_template(**{"value-name": " Ana \n Maria "})
        self.assertRedirects(
            response, reverse("contact_detail", args=[self.contact.pk]) + "#communications"
        )
        self.assertContains(response, "Message sent through Client messages.")
        sent = Communication.objects.get()
        self.assertEqual(
            sent.summary,
            "Hello Ana Maria, this is your law firm. Please reply so we can talk about your case.",
        )
        self.assertEqual((sent.direction, sent.delivery_status), ("outbound", "sent"))
        request = OutboundRequest.objects.get()
        self.assertEqual(
            (request.action, request.summary),
            ("send_template", "Template reply_request from paralegal"),
        )
        self.assertEqual(sent.external_id, request.provider_ref)
        self.assertContains(
            self.send_template("office_closed:en"), "Message sent through Client messages."
        )
        self.assertEqual(Communication.objects.count(), 2)

    def test_while_the_window_is_open_the_page_says_so(self):
        self.wrote(timedelta(minutes=5))
        page = self.template_page()
        self.assertContains(page, "so a text from the contact page works too")
        Communication.objects.update(occurred_at=timezone.now() - timedelta(hours=30))
        self.assertContains(self.template_page(), "last message, on")

    def test_template_problems_are_explained(self):
        response = self.send_template(**{"value-name": "  "})
        self.assertContains(response, "Fill in Client&#x27;s first name.")
        self.assertContains(response, '<details class="panel inline-add" open>')
        self.assertContains(self.send_template("gone:en"), "That template is no longer approved.")
        self.assertContains(
            self.send_template(**{"value-name": "refuse"}),
            "The message was not sent: The example messaging service refused the message.",
        )
        self.assertContains(
            self.send_template(**{"value-name": "x" * 1600}), "at most 1600 characters"
        )
        self.assertFalse(Communication.objects.exists())

    def test_messages_that_cannot_go(self):
        refusing = self.connect("example_messaging", "Refusing", api_key="example-refuse")
        page = self.template_page(connection=refusing.pk)
        self.assertContains(page, "The templates could not be loaded: The example messaging")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        self.assertContains(self.template_page(), "is in preview mode")
        self.assertContains(self.send_template(), "so the message was recorded but not sent")
        self.contact.texting_opted_out_at = timezone.now()
        self.contact.save()
        self.assertContains(self.template_page(), "opted out of texts, so no message can be sent")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        self.assertContains(
            self.send_template(), "The message was not sent. The recipient opted out of texts."
        )
        self.contact.texting_opted_out_at = None
        self.contact.phone = "not a number"
        self.contact.save()
        self.assertContains(self.template_page(), "Add a valid mobile number")
        self.assertContains(self.send_template(), "Add a valid phone number for")
        self.assertFalse(Communication.objects.exists())

    def test_only_connections_the_sender_may_use(self):
        theirs = self.connect(
            "example_messaging", "Other's line", owner=make_user("other", superuser=False)
        )
        self.assertEqual(self.template_page(connection=theirs.pk).status_code, 403)
        self.assertEqual(self.send_template(connection=theirs.pk).status_code, 403)
        texts_only = self.connect("example", "Front desk texts")
        self.assertEqual(self.template_page(connection=texts_only.pk).status_code, 403)

    def test_from_a_matter_page(self):
        matter = make_matter(self.contact, self.staff)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, f"&amp;matter={matter.pk}")
        page = self.template_page(matter=matter.pk)
        self.assertContains(page, reverse("matter_detail", args=[matter.pk]) + "#communications")
        response = self.send_template(matter=matter.pk)
        self.assertRedirects(
            response, reverse("matter_detail", args=[matter.pk]) + "#communications"
        )
        self.assertEqual(Communication.objects.get().matter, matter)
        stranger = make_contact(first_name="Noor", email="noor@example.test", phone="+12025550145")
        self.assertEqual(self.template_page(contact=stranger.pk, matter=matter.pk).status_code, 403)
        MatterParty.objects.create(matter=matter, contact=stranger, role="beneficiary")
        self.assertEqual(self.template_page(contact=stranger.pk, matter=matter.pk).status_code, 200)
