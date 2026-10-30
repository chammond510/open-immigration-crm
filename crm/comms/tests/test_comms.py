from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from crm.comms.forms import CommunicationForm
from crm.comms.models import Communication
from crm.comms.services import record_communication, set_texting_opt_out
from crm.connect.models import Connection, OutboundRequest
from crm.connect.policy import Codes, send
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.models import Activity, AuditLog, MatterParty
from crm.tests.factories import make_contact, make_matter, make_user


def log(contact=None, matter=None, **overrides):
    options = {"channel": Communication.Channel.CALL, "subject": "Status call", **overrides}
    return Communication.objects.create(contact=contact, matter=matter, **options)


class CommunicationModelTests(TestCase):
    def test_duration_display_and_str(self):
        contact = make_contact()
        call = log(contact, duration=timedelta(minutes=45))
        self.assertEqual(call.duration_display, "45 min")
        call.duration = timedelta(minutes=65)
        self.assertEqual(call.duration_display, "1h 05m")
        call.duration = None
        self.assertEqual(call.duration_display, "")
        self.assertEqual(str(call), "Status call")
        untitled = log(contact, subject="", channel=Communication.Channel.TEXT)
        self.assertTrue(str(untitled).startswith("Text message on "))

    def test_must_link_to_a_record(self):
        with self.assertRaises(ValidationError):
            Communication(channel=Communication.Channel.EMAIL).clean()


class CommunicationFormTests(TestCase):
    def setUp(self):
        self.contact = make_contact()
        self.matter = make_matter(self.contact)

    def form(self, **data):
        base = {
            "channel": "call",
            "direction": "outbound",
            "occurred_at": "2026-01-05T10:00",
            "duration_minutes": "20",
        }
        return CommunicationForm({**base, **data})

    def test_matter_supplies_its_primary_contact_and_minutes_become_duration(self):
        form = self.form(matter=str(self.matter.pk))
        self.assertTrue(form.is_valid(), form.errors)
        communication = form.save()
        self.assertEqual(communication.contact, self.contact)
        self.assertEqual(communication.duration, timedelta(minutes=20))
        edit = CommunicationForm(instance=communication)
        self.assertEqual(edit.fields["duration_minutes"].initial, 20)

    def test_choices_have_no_placeholder_dashes(self):
        form = CommunicationForm()
        self.assertNotIn("", dict(form.fields["channel"].choices))
        self.assertEqual(dict(form.fields["direction"].choices)[""], "—")

    def test_links_are_validated(self):
        self.assertIn("Choose the contact", str(self.form().errors))
        stranger = make_contact(first_name="Omar", email="omar@example.test")
        form = self.form(matter=str(self.matter.pk), contact=str(stranger.pk))
        self.assertIn("not a party", str(form.errors))
        MatterParty.objects.create(matter=self.matter, contact=stranger, role="Spouse")
        form = self.form(matter=str(self.matter.pk), contact=str(stranger.pk), duration_minutes="")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().duration)


class CommunicationViewTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin)
        self.client.force_login(self.staff)

    def test_panels_appear_on_contact_and_matter_pages(self):
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Log a call, text, email, or meeting")
        self.assertContains(page, "Connect your phone system to log calls automatically.")
        self.assertContains(page, "Texts allowed")
        self.assertContains(page, 'href="/communications/"')
        log(self.contact, self.matter, subject="Interview prep call")
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Interview prep call")
        self.assertNotContains(page, "Connect your phone system")
        activity_kinds = dict(Activity.Kind.choices)
        self.assertNotIn("call", activity_kinds)

    def test_inline_log_returns_to_the_page(self):
        next_url = reverse("matter_detail", args=[self.matter.pk]) + "#communications"
        response = self.client.post(
            reverse("communication_create"),
            {
                "channel": "meeting",
                "occurred_at": "2026-01-05T10:00",
                "duration_minutes": "30",
                "subject": "Document review meeting",
                "matter": str(self.matter.pk),
                "next": next_url,
            },
        )
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        meeting = Communication.objects.get()
        self.assertEqual(meeting.logged_by, self.staff)
        self.assertEqual(meeting.contact, self.contact)
        self.assertTrue(AuditLog.objects.filter(action="communication.logged").exists())

    def test_log_page_and_unsafe_next(self):
        page = self.client.get(reverse("communication_create"), {"contact": self.contact.pk})
        self.assertContains(page, "Log a communication")
        self.assertEqual(page.context["form"].initial["contact"], self.contact)
        response = self.client.post(
            reverse("communication_create"),
            {
                "channel": "email",
                "direction": "inbound",
                "occurred_at": "2026-01-05T10:00",
                "contact": str(self.contact.pk),
                "next": "https://elsewhere.test/",
            },
        )
        self.assertRedirects(
            response,
            reverse("contact_detail", args=[self.contact.pk]),
            fetch_redirect_response=False,
        )
        invalid = self.client.post(reverse("communication_create"), {"channel": "email"})
        self.assertEqual(invalid.status_code, 200)

    def test_outbound_text_to_opted_out_contact_is_flagged(self):
        set_texting_opt_out(self.contact, True)
        response = self.client.post(
            reverse("communication_create"),
            {
                "channel": "text",
                "direction": "outbound",
                "occurred_at": "2026-01-05T10:00",
                "contact": str(self.contact.pk),
            },
            follow=True,
        )
        self.assertContains(response, "has opted out of texts")

    def test_list_filters(self):
        log(self.contact, self.matter, subject="Fee question", direction="inbound")
        other = make_contact(first_name="Omar", email="omar@example.test")
        log(other, channel="email", subject="Passport scan", counterpart="omar@example.test")
        url = reverse("communication_list")
        page = self.client.get(url)
        self.assertContains(page, "Fee question")
        self.assertContains(page, "Passport scan")
        self.assertNotContains(self.client.get(url, {"channel": "email"}), "Fee question")
        self.assertNotContains(self.client.get(url, {"direction": "inbound"}), "Passport scan")
        self.assertNotContains(self.client.get(url, {"q": "omar@"}), "Fee question")
        page = self.client.get(url, {"contact": self.contact.pk})
        self.assertContains(page, f'For <a href="/contacts/{self.contact.pk}/">')
        self.assertNotContains(page, "Passport scan")
        page = self.client.get(url, {"matter": self.matter.pk})
        self.assertContains(page, "Fee question")
        self.assertContains(self.client.get(url, {"q": "nothing matches"}), "No communications")

    def test_delete_is_superuser_only(self):
        call = log(self.contact)
        url = reverse("communication_delete", args=[call.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertRedirects(
            self.client.post(url), reverse("communication_list"), fetch_redirect_response=False
        )
        self.assertFalse(Communication.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="communication.deleted").exists())

    def test_texting_opt_out_toggle(self):
        url = reverse("contact_texting_opt_out", args=[self.contact.pk])
        self.client.post(url, {"opted_out": "yes"})
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.texting_opted_out_at)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Opted out of texts")
        self.client.post(url, {"opted_out": "yes"})
        self.assertEqual(AuditLog.objects.filter(action="contact.texting_opted_out").count(), 1)
        self.client.post(url, {"opted_out": "no"})
        self.contact.refresh_from_db()
        self.assertIsNone(self.contact.texting_opted_out_at)
        self.assertTrue(AuditLog.objects.filter(action="contact.texting_opted_in").exists())

    def test_staff_only(self):
        self.client.logout()
        response = self.client.get(reverse("communication_list"))
        self.assertEqual(response.status_code, 302)


class RecordCommunicationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.connection = create_connection(
            "example",
            label="Texts",
            created_by=self.user,
            capabilities=["texting"],
            credentials={"api_key": "example-fictional-key"},
        )

    def test_connector_records_are_stored_once(self):
        first = record_communication(
            source_connection=self.connection,
            external_id="msg-1",
            channel="text",
            direction="inbound",
            contact=self.contact,
            summary="Fictional reply",
        )
        again = record_communication(
            source_connection=self.connection,
            external_id="msg-1",
            channel="text",
            direction="inbound",
            contact=self.contact,
            summary="Fictional reply, edited",
        )
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Communication.objects.get().summary, "Fictional reply, edited")
        record_communication(channel="call", contact=self.contact)
        record_communication(channel="call", contact=self.contact)
        self.assertEqual(Communication.objects.count(), 3)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Communication.objects.create(
                source_connection=self.connection, external_id="msg-1", channel="text"
            )

    def test_policy_blocks_texts_to_opted_out_recipients(self):
        acknowledge_data_flows(self.connection, self.user)
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        self.connection.refresh_from_db()
        matter = make_matter(self.contact)
        sent = send(self.connection, "send_message", {"body": "Hi"}, target=self.contact)
        self.assertEqual(sent.status, OutboundRequest.Status.SENT)
        set_texting_opt_out(self.contact, True, actor=self.user, reason="Replied STOP")
        for target in (self.contact, matter):
            blocked = send(self.connection, "send_message", {"body": "Hi"}, target=target)
            self.assertEqual(blocked.decision_code, Codes.RECIPIENT_OPTED_OUT)
        entry = AuditLog.objects.get(action="contact.texting_opted_out")
        self.assertEqual(entry.detail, "Replied STOP")


class MoveActivitiesMigrationTests(TransactionTestCase):
    # Pin crm too, so later crm migrations are unapplied while the historical models are used.
    crm = ("crm", "0005_texting_opt_out_and_activity_kinds")
    before = [("comms", "0001_communications"), crm]
    after = [("comms", "0002_move_call_and_meeting_activities"), crm]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_calls_and_meetings_move_to_the_log_and_back(self):
        apps = self.migrate(self.before)
        Contact = apps.get_model("crm", "Contact")
        Activity = apps.get_model("crm", "Activity")
        contact = Contact.objects.create(first_name="Amina", last_name="Example")
        when = timezone.now() - timedelta(days=2)
        Activity.objects.create(
            contact=contact, kind="call", subject="Intake call", body="Notes", occurred_at=when
        )
        Activity.objects.create(contact=contact, kind="meeting", subject="Consult")
        Activity.objects.create(contact=contact, kind="note", subject="Stays a note")

        apps = self.migrate(self.after)
        Communication = apps.get_model("comms", "Communication")
        moved = Communication.objects.get(channel="call")
        self.assertEqual((moved.subject, moved.summary), ("Intake call", "Notes"))
        self.assertEqual(moved.occurred_at, when)
        self.assertTrue(Communication.objects.filter(channel="meeting").exists())
        Activity = apps.get_model("crm", "Activity")
        self.assertEqual(list(Activity.objects.values_list("kind", flat=True)), ["note"])

        apps = self.migrate(self.before)
        Activity = apps.get_model("crm", "Activity")
        self.assertEqual(
            sorted(Activity.objects.values_list("kind", flat=True)), ["call", "meeting", "note"]
        )
        self.assertFalse(apps.get_model("comms", "Communication").objects.exists())
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
