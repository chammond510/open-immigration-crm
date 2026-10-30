import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from crm.api.models import ApiSettings, ApiToken
from crm.billing.models import Invoice
from crm.billing.services import add_line, issue_invoice, record_payment
from crm.comms.services import record_communication
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest
from crm.connect.registry import manifest_problems, registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.events.catalog import DETAIL_FULL, EVENT_TYPES
from crm.events.models import EventDelivery
from crm.events.services import emit
from crm.events.tasks import MAX_ATTEMPTS, attempt, deliver_event
from crm.models import Activity, AuditLog, IntakeForm, IntakeInvite, IntakeSubmission, WorkItem
from crm.notices.services import record_status
from crm.tests.factories import make_contact, make_matter, make_user

# Core reaches connectors only through the registry, never by import.
WebhooksConnector = registry.get("webhooks")
RECEIPT = "IOE0912345678"


class EventTestCase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = create_connection(
            "webhooks",
            label="Fictional automation",
            created_by=self.user,
            capabilities=["automation"],
            credentials={
                "url": "https://hooks.example.test/fictional",
                "signing_secret": "fictional-signing-secret",
            },
        )
        acknowledge_data_flows(self.connection, self.user)
        self.connection.status = Connection.Status.ACTIVE
        self.connection.preview_mode = False
        self.subscribe(*EVENT_TYPES)

    def subscribe(self, *events, detail="ids"):
        self.connection.config = {
            "events": list(events),
            "detail": detail,
            "site_url": "https://crm.example.test",
        }
        self.connection.save()

    def deliveries(self, event_type=None):
        deliveries = EventDelivery.objects.all()
        if event_type:
            deliveries = deliveries.filter(event_type=event_type)
        return deliveries

    def event_types(self):
        return sorted(self.deliveries().values_list("event_type", flat=True))


class EmitTests(EventTestCase):
    def test_nothing_is_queued_without_a_subscription(self):
        self.subscribe()
        with self.captureOnCommitCallbacks(execute=True):
            make_contact(self.user)
        self.assertFalse(self.deliveries().exists())
        self.connection.enabled_capabilities = []
        self.connection.save()
        self.subscribe("contact.created")
        with self.captureOnCommitCallbacks(execute=True):
            make_contact(self.user, first_name="Ben")
        self.assertFalse(self.deliveries().exists())
        with self.assertRaises(ValueError):
            emit("contact.deleted", make_contact(first_name="Cy"))

    def test_events_wait_for_commit_and_carry_ids_and_links(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            contact = make_contact(self.user)
        self.assertFalse(self.deliveries().exists())
        for callback in callbacks:
            callback()
        delivery = self.deliveries("contact.created").get()
        self.assertEqual((delivery.target_type, delivery.target_id), ("Contact", str(contact.pk)))
        payload = delivery.payload
        self.assertEqual(payload["type"], "contact.created")
        self.assertEqual(payload["data"]["id"], str(contact.pk))
        self.assertEqual(payload["data"]["via"], "crm")
        self.assertEqual(
            payload["data"]["web_url"], f"https://crm.example.test/contacts/{contact.pk}/"
        )
        self.assertEqual(
            payload["data"]["api_url"], f"https://crm.example.test/api/v1/contacts/{contact.pk}/"
        )
        self.assertNotIn("record", payload["data"])
        self.assertNotIn(contact.first_name, json.dumps(payload))
        task = DBTaskResult.objects.get(task_path=deliver_event.module_path)
        self.assertEqual(task.args_kwargs["args"], [str(delivery.pk)])

    def test_full_detail_includes_the_record(self):
        self.subscribe("contact.created", detail=DETAIL_FULL)
        with self.captureOnCommitCallbacks(execute=True):
            make_contact(self.user)
        record = self.deliveries().get().payload["data"]["record"]
        self.assertEqual(record["first_name"], "Amina")

    def test_one_event_per_object_per_transaction(self):
        with self.captureOnCommitCallbacks(execute=True):
            contact = make_contact(self.user)
            contact.phone = "555-0100"
            contact.save()
        self.assertEqual(self.event_types(), ["contact.created"])
        with self.captureOnCommitCallbacks(execute=True):
            contact.city = "Fictional City"
            contact.save()
            contact.save()
        self.assertEqual(self.event_types(), ["contact.created", "contact.updated"])

    def test_matter_status_and_work_item_events(self):
        contact = make_contact(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            matter = make_matter(contact, self.user)
        EventDelivery.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            matter.title = "Renamed"
            matter.save()
        self.assertEqual(self.event_types(), ["matter.updated"])
        with self.captureOnCommitCallbacks(execute=True):
            matter.refresh_from_db()
            matter.status = "approved"
            matter.save()
        changed = self.deliveries("matter.status_changed").get()
        self.assertEqual(
            changed.payload["data"]["changes"], {"status": {"from": "open", "to": "approved"}}
        )
        self.assertEqual(changed.payload["data"]["contact"], str(contact.pk))

        with self.captureOnCommitCallbacks(execute=True):
            item = WorkItem.objects.create(title="Call", matter=matter)
        with self.captureOnCommitCallbacks(execute=True):
            item.is_completed = True
            item.save()
            item.save()
        self.assertEqual(self.deliveries("work_item.created").count(), 1)
        completed = self.deliveries("work_item.completed").get()
        self.assertEqual(completed.payload["data"]["matter"], str(matter.pk))

    def test_notes_communications_notices_and_status(self):
        contact = make_contact(self.user)
        matter = make_matter(contact, self.user)
        EventDelivery.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            Activity.objects.create(contact=contact, subject="Note")
            Activity.objects.create(contact=contact, subject="System", kind="system")
            record_communication(channel="call", contact=contact, logged_by=self.user)
            record_status(matter, receipt_number=RECEIPT, status_title="Case Was Received")
        with self.captureOnCommitCallbacks(execute=True):
            record_status(matter, receipt_number=RECEIPT, status_title="Case Was Received")
        with self.captureOnCommitCallbacks(execute=True):
            record_status(matter, receipt_number=RECEIPT, status_title="Case Was Approved")
        self.assertEqual(
            self.event_types(),
            [
                "case_status.changed",
                "case_status.changed",
                "communication.logged",
                "note.created",
            ],
        )

    def test_intake_and_billing_events(self):
        contact = make_contact(self.user)
        form = IntakeForm.objects.create(name="Fictional intake", questions=[])
        invite, _ = IntakeInvite.issue(
            intake_form=form,
            contact=contact,
            expires_at=timezone.now() + timedelta(days=1),
            created_by=self.user,
        )
        invoice = Invoice.objects.create(contact=contact, created_by=self.user)
        add_line(invoice, description="Fictional fee", unit_price=Decimal("100"))
        EventDelivery.objects.all().delete()
        self.subscribe("intake.submitted", "invoice.issued", "payment.recorded", detail="full")
        with self.captureOnCommitCallbacks(execute=True):
            IntakeSubmission.objects.create(invite=invite, answers={"q": "Fictional"})
            issue_invoice(invoice, self.user)
            record_payment(
                invoice,
                self.user,
                amount="40",
                method="check",
                deposit_account="operating",
                received_on=date(2026, 3, 2),
            )
        self.assertEqual(
            self.event_types(), ["intake.submitted", "invoice.issued", "payment.recorded"]
        )
        submission = self.deliveries("intake.submitted").get().payload["data"]
        self.assertEqual(submission["record"]["form"], "Fictional intake")
        self.assertIn("/intake/submissions/", submission["web_url"])
        payment = self.deliveries("payment.recorded").get().payload["data"]
        self.assertEqual(payment["record"]["amount"], "40.00")
        self.assertEqual(payment["invoice"], str(invoice.pk))

    def test_changes_through_the_api_are_marked(self):
        ApiSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        _, raw_token = ApiToken.issue(
            user=self.user, name="Fictional", scopes=["contacts:write"], lifetime_days=30
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/contacts/",
                json.dumps({"first_name": "Lena", "last_name": "Example"}),
                content_type="application/json",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        data = self.deliveries("contact.created").get().payload["data"]
        self.assertEqual((data["via"], data["api_token"]), ("api", raw_token[:12]))


class DeliveryTests(EventTestCase):
    def queue(self):
        with self.captureOnCommitCallbacks(execute=True):
            make_contact(self.user)
        return self.deliveries("contact.created").get()

    def test_a_sent_delivery_clears_its_payload(self):
        delivery = self.queue()
        with mock.patch.object(WebhooksConnector, "perform", return_value="HTTP 204") as perform:
            deliver_event.call(str(delivery.pk))
        action, payload = perform.call_args.args
        self.assertEqual((action, payload["id"]), ("deliver_event", str(delivery.pk)))
        self.assertEqual(payload["event"]["type"], "contact.created")
        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.attempts), ("sent", 1))
        self.assertIsNone(delivery.payload)
        outbound = OutboundRequest.objects.get()
        self.assertEqual((outbound.status, outbound.summary), ("sent", "contact.created"))
        self.assertTrue(outbound.is_automation)
        with mock.patch.object(WebhooksConnector, "perform") as perform:
            deliver_event.call(str(delivery.pk))
        perform.assert_not_called()

    def test_preview_mode_and_blocked_deliveries(self):
        self.connection.preview_mode = True
        self.connection.save()
        delivery = attempt(self.queue())
        self.assertEqual(delivery.status, "previewed")
        self.connection.preview_mode = False
        self.connection.data_flows_version = None
        self.connection.save()
        with self.captureOnCommitCallbacks(execute=True):
            make_contact(self.user, first_name="Ben")
        blocked = attempt(self.deliveries("contact.created").filter(status="queued").get())
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("review", blocked.last_error)
        self.assertIsNone(blocked.payload)

    def test_failures_retry_with_backoff_then_give_up(self):
        delivery = self.queue()
        error = ConnectorError("The receiver answered HTTP 500.")
        with mock.patch.object(WebhooksConnector, "perform", side_effect=error):
            delivery = attempt(delivery)
            self.assertEqual(delivery.status, "retrying")
            self.assertGreater(delivery.next_attempt_at, timezone.now())
            self.assertIsNotNone(delivery.payload)
            retry = DBTaskResult.objects.filter(task_path=deliver_event.module_path).latest(
                "enqueued_at"
            )
            self.assertIsNotNone(retry.run_after)
            for _ in range(MAX_ATTEMPTS - 1):
                delivery = attempt(delivery)
        self.assertEqual((delivery.status, delivery.attempts), ("failed", MAX_ATTEMPTS))
        self.assertEqual(delivery.last_error, "The receiver answered HTTP 500.")
        self.assertIsNone(delivery.payload)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.last_error, "The receiver answered HTTP 500.")

    @override_settings(CONNECTORS_OUTBOUND_HOURLY_LIMIT=0)
    def test_rate_limited_deliveries_retry(self):
        delivery = attempt(self.queue())
        self.assertEqual(delivery.status, "retrying")


class EventSettingsTests(EventTestCase):
    def test_superusers_choose_events(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Events to send")
        self.assertContains(page, "matter.status_changed")
        self.assertNotContains(page, "Recent syncs")
        url = reverse("connection_events", args=[self.connection.pk])
        response = self.client.post(
            url, {"events": ["matter.status_changed", "contact.created"], "detail": "ids"}
        )
        self.assertRedirects(
            response,
            reverse("connection_detail", args=[self.connection.pk]),
            fetch_redirect_response=False,
        )
        self.connection.refresh_from_db()
        self.assertEqual(
            self.connection.config,
            {
                "events": ["contact.created", "matter.status_changed"],
                "detail": "ids",
                "site_url": "http://testserver",
            },
        )
        audit = AuditLog.objects.get(action="connection.events_changed")
        self.assertEqual(audit.detail, "2 event types, ids detail")
        response = self.client.post(url, {"detail": "everything"}, follow=True)
        self.assertContains(response, "Choose what each event includes.")
        response = self.client.post(url, {"detail": "ids"}, follow=True)
        self.assertContains(response, "nothing will be sent")

    def test_staff_see_choices_but_cannot_change_them(self):
        staff = make_user("paralegal", superuser=False)
        self.client.force_login(staff)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Chosen by a superuser.")
        self.assertContains(page, "A matter&#x27;s status changes")
        response = self.client.post(reverse("connection_events", args=[self.connection.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_connections_without_automation_are_refused(self):
        self.connection.enabled_capabilities = []
        self.connection.save()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("connection_events", args=[self.connection.pk]), {"detail": "ids"}, follow=True
        )
        self.assertContains(response, "does not send automation events")


class FrameworkTests(TestCase):
    def test_automation_connectors_must_deliver_events(self):
        class Incomplete(WebhooksConnector):
            outbound_actions = {}

        self.assertIn(
            "automation connectors must declare the deliver_event outbound action",
            manifest_problems(Incomplete),
        )

    def test_connect_form_offers_a_generated_secret(self):
        self.client.force_login(make_user())
        page = self.client.get(reverse("connector_detail", args=["webhooks"]))
        field = page.context["form"]["credential_signing_secret"]
        self.assertTrue(str(field.value()).startswith("whsec_"))
        self.assertEqual(field.field.widget.input_type, "text")
        self.assertContains(page, "Webhook address")
        catalog = self.client.get(reverse("connections"), {"capability": "automation"})
        self.assertContains(catalog, "Webhooks")
        self.assertContains(catalog, "Webhook address")
