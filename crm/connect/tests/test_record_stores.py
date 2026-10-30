from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from crm.billing.models import Invoice, Payment
from crm.billing.services import add_line, issue_invoice
from crm.comms.models import Communication
from crm.connect import refs
from crm.connect.models import Connection, ExternalRef
from crm.connect.records import (
    BookingRecord,
    CalendarEventRecord,
    CallRecord,
    CaseStatusRecord,
    EmailRecord,
    PaymentRecord,
    TextRecord,
)
from crm.connect.signals import records_received
from crm.models import Activity, AuditLog, Contact, WorkItem
from crm.notices.models import CaseNotice, CaseStatusCheck
from crm.tests.factories import make_contact, make_matter, make_user

AT = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
NUMBER = "+12025550143"


def deliver(connection, *records, capability="phone"):
    records_received.send(
        sender=None,
        connection=connection,
        capability=capability,
        records=list(records),
        source="sync",
    )


class StoreTestCase(TestCase):
    def setUp(self):
        self.staff = make_user("paralegal", superuser=False)
        self.connection = Connection.objects.create(
            connector_key="example", label="Fictional service", enabled_capabilities=["texting"]
        )


class CommunicationStoreTests(StoreTestCase):
    def test_calls_texts_and_emails(self):
        lena = make_contact(first_name="Lena", email="lena@example.test", phone="(202) 555-0143")
        deliver(
            self.connection,
            CallRecord(
                external_id="call-1",
                occurred_at=AT,
                direction="inbound",
                remote="202-555-0143",
                duration_seconds=125,
                outcome="completed",
                summary="Fictional recap.",
                recording_url="https://recordings.example.test/1",
                staff_email="paralegal@example.test",
            ),
            TextRecord(
                external_id="text-1",
                occurred_at=AT,
                direction="outbound",
                remote=NUMBER,
                body="Fictional reminder",
            ),
            EmailRecord(
                external_id="mail-1",
                occurred_at=AT,
                direction="inbound",
                remote="LENA@example.test",
                subject="Documents",
                snippet="Attached.",
            ),
        )
        call = Communication.objects.get(external_id="call-1")
        self.assertEqual((call.contact, call.logged_by, call.channel), (lena, self.staff, "call"))
        self.assertEqual(call.duration, timedelta(seconds=125))
        self.assertEqual(call.subject, "Call completed")
        self.assertIn("Recording: https://recordings.example.test/1", call.summary)
        self.assertEqual(call.counterpart_key, NUMBER)
        text = Communication.objects.get(external_id="text-1")
        self.assertEqual(
            (text.channel, text.summary, text.contact), ("text", "Fictional reminder", lena)
        )
        email = Communication.objects.get(external_id="mail-1")
        self.assertEqual((email.subject, email.contact), ("Documents", lena))
        deliver(
            self.connection,
            TextRecord(
                external_id="text-1",
                occurred_at=AT,
                direction="outbound",
                remote=NUMBER,
                body="Edited",
            ),
        )
        self.assertEqual(Communication.objects.filter(external_id="text-1").count(), 1)

    def test_unmatched_records_wait_and_links_are_kept(self):
        deliver(
            self.connection,
            TextRecord(
                external_id="t1", occurred_at=AT, direction="inbound", remote=NUMBER, body="Hello"
            ),
        )
        item = Communication.objects.get()
        self.assertIsNone(item.contact)
        omar = make_contact(first_name="Omar", email="omar@example.test")
        Communication.objects.filter(pk=item.pk).update(contact=omar)
        deliver(
            self.connection,
            TextRecord(
                external_id="t1", occurred_at=AT, direction="inbound", remote=NUMBER, body="Hello"
            ),
        )
        item.refresh_from_db()
        self.assertEqual(item.contact, omar)

    def test_contacts_saved_with_the_number_pick_up_unmatched_records(self):
        deliver(
            self.connection,
            CallRecord(external_id="c1", occurred_at=AT, direction="inbound", remote=NUMBER),
            CallRecord(
                external_id="c2", occurred_at=AT, direction="inbound", remote="(202) 555-0143"
            ),
        )
        lena = make_contact(first_name="Lena", email="lena@example.test", phone="202 555 0143")
        self.assertEqual(Communication.objects.filter(contact=lena).count(), 2)
        # A number shared by two contacts, as in many families, is never guessed.
        make_contact(first_name="Ana", email="ana@example.test", phone="312 555 0188")
        ben = make_contact(first_name="Ben", email="ben@example.test", phone="312 555 0188")
        deliver(
            self.connection,
            CallRecord(
                external_id="c3", occurred_at=AT, direction="inbound", remote="+13125550188"
            ),
        )
        ben.save()
        self.assertIsNone(Communication.objects.get(external_id="c3").contact)

    def test_stop_and_start_replies(self):
        lena = make_contact(phone="(202) 555-0143")
        deliver(
            self.connection,
            TextRecord(
                external_id="t1", occurred_at=AT, direction="inbound", remote=NUMBER, body=" stop "
            ),
        )
        lena.refresh_from_db()
        self.assertIsNotNone(lena.texting_opted_out_at)
        self.assertEqual(
            AuditLog.objects.get(action="contact.texting_opted_out").detail, "Replied STOP by text"
        )
        deliver(
            self.connection,
            TextRecord(
                external_id="t2", occurred_at=AT, direction="inbound", remote=NUMBER, body="START"
            ),
        )
        lena.refresh_from_db()
        self.assertIsNone(lena.texting_opted_out_at)

    def test_one_bad_record_does_not_stop_the_rest(self):
        outcomes = [RuntimeError("boom"), Communication(channel="text")]
        with mock.patch("crm.comms.records.record_communication", side_effect=outcomes) as stored:
            with self.assertLogs("crm.comms.records", "ERROR") as logs:
                deliver(
                    self.connection,
                    TextRecord(
                        external_id="t1", occurred_at=AT, direction="inbound", remote=NUMBER
                    ),
                    TextRecord(
                        external_id="t2", occurred_at=AT, direction="inbound", remote=NUMBER
                    ),
                )
        self.assertEqual(stored.call_count, 2)
        # Logs name the record kind and connection, never the number or content.
        self.assertNotIn(NUMBER, "".join(logs.output))


class AppointmentStoreTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.lena = make_contact(first_name="Lena", email="lena@example.test")

    def event(self, **fields):
        return CalendarEventRecord(
            **{
                "external_id": "event-1",
                "occurred_at": AT,
                "title": "Fictional consult",
                "attendee_emails": ("paralegal@example.test", "lena@example.test"),
                "staff_email": "paralegal@example.test",
                "location": "Office",
                "etag": "1",
                **fields,
            }
        )

    def test_events_with_a_contact_become_appointments(self):
        deliver(self.connection, self.event(), capability="calendar")
        item = WorkItem.objects.get()
        self.assertEqual(
            (item.kind, item.contact, item.assigned_to), ("appointment", self.lena, self.staff)
        )
        self.assertEqual((item.scheduled_for, item.description), (AT, "Office"))
        deliver(self.connection, self.event(title="Changed but same etag"), capability="calendar")
        item.refresh_from_db()
        self.assertEqual(item.title, "Fictional consult")
        deliver(
            self.connection,
            self.event(title="Moved", occurred_at=AT + timedelta(hours=1), etag="2"),
            capability="calendar",
        )
        item.refresh_from_db()
        self.assertEqual((item.title, item.scheduled_for), ("Moved", AT + timedelta(hours=1)))
        deliver(self.connection, self.event(cancelled=True, etag="3"), capability="calendar")
        item.refresh_from_db()
        self.assertEqual((item.title, item.is_completed), ("Cancelled: Moved", True))
        self.assertEqual(WorkItem.objects.count(), 1)

    def test_personal_events_and_duplicates(self):
        deliver(
            self.connection,
            self.event(external_id="dentist", attendee_emails=("paralegal@example.test",)),
            capability="calendar",
        )
        self.assertFalse(WorkItem.objects.exists())
        deliver(
            self.connection, self.event(external_id="gone", cancelled=True), capability="calendar"
        )
        self.assertFalse(WorkItem.objects.exists())
        by_hand = WorkItem.objects.create(
            kind="appointment",
            title="fictional CONSULT",
            contact=self.lena,
            scheduled_for=AT + timedelta(minutes=1),
        )
        deliver(self.connection, self.event(), capability="calendar")
        self.assertEqual(list(WorkItem.objects.values_list("pk", flat=True)), [by_hand.pk])
        self.assertTrue(ExternalRef.objects.filter(object_id=str(by_hand.pk)).exists())

    def test_bookings_match_or_create_the_contact(self):
        booking = BookingRecord(
            external_id="booking-1",
            occurred_at=AT,
            title="Fictional consultation",
            invitee_name="Ines Example",
            invitee_email="ines@example.test",
            invitee_phone="(202) 555-0177",
            answers={"Case type": "Family"},
        )
        deliver(self.connection, booking, capability="scheduling")
        ines = Contact.objects.get(email="ines@example.test")
        self.assertEqual(
            (ines.first_name, ines.last_name, ines.stage), ("Ines", "Example", "consultation")
        )
        self.assertEqual(ines.phone_e164, "+12025550177")
        self.assertTrue(
            Activity.objects.filter(
                contact=ines, subject__startswith="Contact created from a booking"
            ).exists()
        )
        item = WorkItem.objects.get()
        self.assertEqual((item.contact, item.description), (ines, "Case type: Family"))
        known = BookingRecord(
            external_id="booking-2",
            occurred_at=AT,
            title="Follow-up",
            invitee_email="LENA@example.test",
        )
        deliver(self.connection, known, capability="scheduling")
        self.assertEqual(WorkItem.objects.get(title="Follow-up").contact, self.lena)
        deliver(
            self.connection,
            BookingRecord(
                external_id="booking-1",
                occurred_at=AT,
                title="Fictional consultation",
                invitee_email="ines@example.test",
                cancelled=True,
            ),
            capability="scheduling",
        )
        self.assertTrue(WorkItem.objects.get(pk=item.pk).is_completed)
        self.assertEqual(Contact.objects.filter(email="ines@example.test").count(), 1)

    def test_stale_refs_are_dropped(self):
        deliver(self.connection, self.event(), capability="calendar")
        WorkItem.objects.all().delete()
        self.assertEqual(refs.find(self.connection, WorkItem, "event-1"), (None, None))
        self.assertFalse(ExternalRef.objects.exists())


class PaymentStoreTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.contact = make_contact()
        self.invoice = Invoice.objects.create(contact=self.contact, created_by=self.staff)
        add_line(self.invoice, description="Fictional fee", unit_price=Decimal("500"))
        self.invoice = issue_invoice(self.invoice, self.staff)

    def payment(self, **fields):
        return PaymentRecord(
            **{
                "external_id": "charge-1",
                "occurred_at": AT,
                "amount": "200.00",
                "reference": self.invoice.number,
                **fields,
            }
        )

    def test_payments_are_recorded_once_and_refunds_void_them(self):
        deliver(self.connection, self.payment(), self.payment(), capability="payments")
        payment = Payment.objects.get()
        self.assertEqual(
            (payment.amount, payment.deposit_account, payment.method),
            (Decimal("200.00"), "operating", "card"),
        )
        self.assertEqual(
            (payment.source_connection, payment.received_on), (self.connection, date(2026, 3, 2))
        )
        deliver(self.connection, self.payment(status="refunded"), capability="payments")
        payment.refresh_from_db()
        self.assertIsNotNone(payment.voided_at)
        deliver(
            self.connection,
            self.payment(external_id="failed", status="failed"),
            capability="payments",
        )
        self.assertEqual(Payment.objects.count(), 1)

    def test_trust_money_is_never_guessed(self):
        draft = Invoice.objects.create(contact=self.contact, created_by=self.staff)
        add_line(draft, description="Fictional advance", unit_price=Decimal("300"), funds="advance")
        draft = issue_invoice(draft, self.staff)
        deliver(
            self.connection,
            self.payment(external_id="a", reference=draft.number),
            capability="payments",
        )
        self.assertFalse(Payment.objects.exists())
        self.assertIn(
            "operating or trust", AuditLog.objects.get(action="payment.needs_review").detail
        )
        deliver(
            self.connection,
            self.payment(external_id="b", reference=draft.number, deposit_account="trust"),
            capability="payments",
        )
        self.assertEqual(Payment.objects.get().deposit_account, "trust")

    def test_unmatched_and_refused_payments_are_noted(self):
        deliver(self.connection, self.payment(reference="NOPE-1"), capability="payments")
        deliver(
            self.connection, self.payment(external_id="big", amount="900.00"), capability="payments"
        )
        self.assertTrue(AuditLog.objects.filter(action="payment.unmatched").exists())
        self.assertIn("balance", AuditLog.objects.get(action="payment.not_recorded").detail)
        self.assertFalse(Payment.objects.exists())


class CaseStatusStoreTests(StoreTestCase):
    def test_status_readings_find_their_matter(self):
        matter = make_matter(make_contact(), receipt_number="IOE0912345678")
        other = make_matter(make_contact(first_name="Ben", email="ben@example.test"))
        CaseNotice.objects.create(
            matter=other,
            kind="receipt",
            receipt_number="EAC2190012345",
            notice_date=date(2026, 1, 5),
        )
        deliver(
            self.connection,
            CaseStatusRecord(
                external_id="s1",
                occurred_at=AT,
                receipt_number="ioe0912345678",
                status_title="Case Was Received",
            ),
            CaseStatusRecord(
                external_id="s2",
                occurred_at=AT,
                receipt_number="EAC2190012345",
                status_title="Case Was Approved",
            ),
            CaseStatusRecord(
                external_id="s3",
                occurred_at=AT,
                receipt_number="WAC2190000001",
                status_title="Unknown",
            ),
            capability="case_status",
        )
        self.assertEqual(
            CaseStatusCheck.objects.get(matter=matter).source_connection, self.connection
        )
        self.assertEqual(
            CaseStatusCheck.objects.get(matter=other).status_title, "Case Was Approved"
        )
        self.assertEqual(CaseStatusCheck.objects.count(), 2)


class NeedsMatchingPageTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        deliver(
            self.connection,
            TextRecord(
                external_id="t1", occurred_at=AT, direction="inbound", remote=NUMBER, body="Hi"
            ),
            TextRecord(
                external_id="t2",
                occurred_at=AT,
                direction="inbound",
                remote="(202) 555-0143",
                body="Again",
            ),
        )
        self.client.force_login(self.staff)

    def test_list_match_and_create(self):
        listing = self.client.get(reverse("communication_list"))
        self.assertContains(listing, "2 communications from connected services match no contact.")
        only = self.client.get(reverse("communication_list"), {"matching": "needed"})
        self.assertContains(only, "Showing communications that match no contact.")
        item = Communication.objects.get(external_id="t1")
        self.assertContains(only, reverse("communication_match", args=[item.pk]))
        page = self.client.get(reverse("communication_match", args=[item.pk]), {"q": "Lena"})
        self.assertContains(page, "No contacts match")
        self.assertContains(page, "phone=%2B12025550143")
        lena = make_contact(first_name="Lena", email="lena@example.test")
        page = self.client.get(reverse("communication_match", args=[item.pk]), {"q": "Lena"})
        self.assertContains(page, "Lena Example")
        self.assertContains(page, "and 1 more from this number")
        response = self.client.post(
            reverse("communication_match", args=[item.pk]),
            {"contact": lena.pk, "include_same": "on"},
            follow=True,
        )
        self.assertContains(response, "Linked 2 communications to Lena Example.")
        self.assertEqual(Communication.objects.filter(contact=lena).count(), 2)
        self.assertEqual(
            self.client.get(reverse("communication_match", args=[item.pk])).status_code, 404
        )
        form = self.client.get(reverse("contact_create"), {"phone": NUMBER})
        self.assertEqual(form.context["form"]["phone"].value(), NUMBER)

    def test_link_only_one(self):
        item = Communication.objects.get(external_id="t1")
        omar = make_contact(first_name="Omar", email="omar@example.test")
        self.client.post(reverse("communication_match", args=[item.pk]), {"contact": omar.pk})
        self.assertEqual(Communication.objects.filter(contact=omar).count(), 1)
