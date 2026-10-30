from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.utils import timezone

from crm.billing.models import Invoice
from crm.billing.services import add_line, issue_invoice, record_payment
from crm.comms.models import Communication
from crm.models import Activity, AuditLog, MatterParty, TimeEntry, WorkItem
from crm.notices.models import CaseNotice
from crm.tests.factories import make_contact, make_matter, make_user

from .helpers import ApiTestCase

RECEIPT = "IOE0912345678"


def fields(response):
    return response.json()["error"]["fields"]


class ContactEndpointTests(ApiTestCase):
    def test_create_with_defaults_activity_and_audit(self):
        response = self.api(
            "post",
            "/api/v1/contacts/",
            {"first_name": "Lena", "last_name": "Example", "email": "lena@example.test"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["full_name"], "Lena Example")
        self.assertEqual(body["assigned_to"], self.user.pk)
        self.assertEqual((body["status"], body["stage"]), ("lead", "inquiry"))
        self.assertTrue(body["web_url"].endswith(f"/contacts/{body['id']}/"))
        self.assertTrue(
            Activity.objects.filter(contact_id=body["id"], subject="Contact created").exists()
        )
        audit = AuditLog.objects.get(action="contact.created")
        self.assertEqual(audit.user, self.user)
        self.assertTrue(audit.detail.startswith("Through API token oic_"))

    def test_validation_errors(self):
        response = self.api("post", "/api/v1/contacts/", {"first_name": "A", "email": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(fields(response)), {"last_name", "email"})
        response = self.api("post", "/api/v1/contacts/", {"first_name": "A", "id": "x"})
        self.assertEqual(response.json()["error"]["code"], "unknown_fields")
        self.assertEqual(fields(response), ["id"])
        response = self.api("post", "/api/v1/contacts/", {"first_name": ["A"], "last_name": "B"})
        self.assertIn("first_name", response.json()["error"]["message"])

    def test_list_filters_and_pages(self):
        for name in ("Ana", "Ben", "Cyd"):
            make_contact(first_name=name, email=f"{name.lower()}@example.test")
        response = self.api("get", "/api/v1/contacts/?limit=2")
        body = response.json()
        self.assertEqual(len(body["results"]), 2)
        self.assertIn("offset=2", body["next"])
        page_two = self.api("get", body["next"]).json()
        self.assertEqual((len(page_two["results"]), page_two["next"]), (1, None))
        results = self.api("get", "/api/v1/contacts/?q=ben").json()["results"]
        self.assertEqual([c["first_name"] for c in results], ["Ben"])
        self.assertEqual(self.api("get", "/api/v1/contacts/?stage=nope").status_code, 400)
        self.assertEqual(self.api("get", "/api/v1/contacts/?limit=500").status_code, 400)
        self.assertEqual(self.api("get", "/api/v1/contacts/?limit=x").status_code, 400)
        later = urlencode({"updated_since": timezone.now().isoformat()})
        self.assertEqual(self.api("get", f"/api/v1/contacts/?{later}").json()["results"], [])
        self.assertEqual(
            len(self.api("get", "/api/v1/contacts/?updated_since=2020-01-01").json()["results"]),
            3,
        )
        self.assertEqual(self.api("get", "/api/v1/contacts/?updated_since=soon").status_code, 400)
        self.assertEqual(self.api("get", "/api/v1/contacts/?assigned_to=me").json()["results"], [])

    def test_patch_changes_only_named_fields(self):
        contact = make_contact(self.user, phone="555-0100")
        response = self.api("patch", f"/api/v1/contacts/{contact.pk}/", {"stage": "consultation"})
        self.assertEqual(response.status_code, 200)
        contact.refresh_from_db()
        self.assertEqual((contact.stage, contact.phone), ("consultation", "555-0100"))
        self.assertTrue(
            Activity.objects.filter(
                contact=contact, subject="Pipeline stage changed to Consultation"
            ).exists()
        )
        self.assertEqual(
            self.api("get", f"/api/v1/contacts/{contact.pk}/").json()["phone"], "555-0100"
        )
        missing = "/api/v1/contacts/00000000-0000-0000-0000-000000000000/"
        self.assertEqual(self.api("get", missing).status_code, 404)


class MatterEndpointTests(ApiTestCase):
    def test_create_list_and_patch(self):
        contact = make_contact(self.user)
        response = self.api(
            "post",
            "/api/v1/matters/",
            {"primary_contact": str(contact.pk), "title": "Fictional petition"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["matter_number"])
        self.assertEqual(body["primary_contact_name"], contact.full_name)
        spouse = make_contact(first_name="Omar", email="omar@example.test")
        MatterParty.objects.create(matter_id=body["id"], contact=spouse, role="Beneficiary")
        results = self.api("get", f"/api/v1/matters/?contact={spouse.pk}").json()["results"]
        self.assertEqual([m["id"] for m in results], [body["id"]])
        self.assertEqual(results[0]["parties"][0]["role"], "Beneficiary")
        self.assertEqual(self.api("get", "/api/v1/matters/?contact=bad").status_code, 400)

        response = self.api("patch", f"/api/v1/matters/{body['id']}/", {"status": "pending"})
        self.assertEqual(response.json()["status"], "pending")
        self.assertTrue(
            Activity.objects.filter(subject="Matter status changed to Pending").exists()
        )
        response = self.api("patch", f"/api/v1/matters/{body['id']}/", {"primary_contact": "x"})
        self.assertIn("primary_contact", fields(response))


class WorkAndNoteEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def test_work_items(self):
        response = self.api("post", "/api/v1/work-items/", {"title": "Call the client"})
        self.assertIn("record", fields(response))
        response = self.api(
            "post",
            "/api/v1/work-items/",
            {
                "title": "File the response",
                "kind": "deadline",
                "matter": str(self.matter.pk),
                "scheduled_for": "2026-06-01T17:00:00-05:00",
            },
        )
        self.assertEqual(response.status_code, 201)
        item = response.json()
        self.assertEqual((item["priority"], item["is_completed"]), ("normal", False))
        response = self.api("patch", f"/api/v1/work-items/{item['id']}/", {"is_completed": "yes"})
        self.assertIn("is_completed", fields(response))
        response = self.api("patch", f"/api/v1/work-items/{item['id']}/", {"is_completed": True})
        self.assertTrue(response.json()["completed_at"])
        self.assertEqual(WorkItem.objects.get().title, "File the response")
        listing = self.api("get", "/api/v1/work-items/?completed=false").json()["results"]
        self.assertEqual(listing, [])
        listing = self.api(
            "get", "/api/v1/work-items/?scheduled_after=2026-05-01&scheduled_before=2026-07-01"
        ).json()["results"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(self.api("get", "/api/v1/work-items/?completed=maybe").status_code, 400)

    def test_notes(self):
        response = self.api(
            "post", "/api/v1/notes/", {"subject": "Called", "matter": str(self.matter.pk)}
        )
        self.assertEqual(response.status_code, 201)
        note = response.json()
        self.assertEqual(note["contact"], str(self.contact.pk))
        self.assertEqual(note["created_by"], self.user.pk)
        other = make_contact(first_name="Omar", email="omar@example.test")
        response = self.api(
            "post",
            "/api/v1/notes/",
            {"subject": "Called", "matter": str(self.matter.pk), "contact": str(other.pk)},
        )
        self.assertIn("contact", fields(response))
        self.assertIn("record", fields(self.api("post", "/api/v1/notes/", {"subject": "x"})))
        listing = self.api("get", f"/api/v1/notes/?matter={self.matter.pk}").json()["results"]
        self.assertEqual([n["id"] for n in listing], [note["id"]])
        self.assertEqual(
            self.api("get", f"/api/v1/notes/{note['id']}/").json()["subject"], "Called"
        )


class CommunicationAndTimeEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.contact = make_contact(self.user)

    def test_communications_are_stored_once_per_external_id(self):
        payload = {
            "channel": "call",
            "direction": "inbound",
            "contact": str(self.contact.pk),
            "duration_minutes": 12,
            "summary": "Fictional call.",
            "external_id": "call-1",
        }
        first = self.api("post", "/api/v1/communications/", payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["duration_minutes"], 12)
        again = self.api("post", "/api/v1/communications/", payload)
        self.assertEqual((again.status_code, again.json()["id"]), (200, first.json()["id"]))
        self.assertEqual(Communication.objects.get().logged_by, self.user)
        response = self.api("post", "/api/v1/communications/", {**payload, "external_id": 5})
        self.assertIn("external_id", fields(response))
        self.assertEqual(
            len(self.api("get", "/api/v1/communications/?channel=call").json()["results"]), 1
        )
        detail = self.api("get", f"/api/v1/communications/{first.json()['id']}/").json()
        self.assertEqual(detail["external_id"], "call-1")

    def test_time_entries(self):
        response = self.api(
            "post",
            "/api/v1/time-entries/",
            {"contact": str(self.contact.pk), "duration_minutes": 30},
        )
        self.assertIn("started_at", fields(response))
        response = self.api(
            "post",
            "/api/v1/time-entries/",
            {
                "contact": str(self.contact.pk),
                "started_at": "2026-03-02T09:00:00Z",
                "duration_minutes": 30,
                "note": "Fictional review",
            },
        )
        self.assertEqual(response.status_code, 201)
        entry = response.json()
        self.assertEqual((entry["duration_minutes"], entry["user"]), (30, self.user.pk))
        for stopped in ("2026-03-02T08:00:00Z", "2026-03-04T09:00:00Z", None):
            response = self.api(
                "post",
                "/api/v1/time-entries/",
                {
                    "contact": str(self.contact.pk),
                    "started_at": "2026-03-02T09:00:00Z",
                    "stopped_at": stopped,
                },
            )
            self.assertIn("stopped_at", fields(response))
        self.assertEqual(TimeEntry.objects.count(), 1)
        listing = self.api("get", "/api/v1/time-entries/?user=me&started_after=2026-03-01")
        self.assertEqual(len(listing.json()["results"]), 1)
        detail = self.api("get", f"/api/v1/time-entries/{entry['id']}/").json()
        self.assertEqual(detail["note"], "Fictional review")


class NoticeEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def test_notices_drive_deadlines(self):
        response = self.api("post", "/api/v1/notices/", {"kind": "rfe"})
        self.assertEqual(fields(response), {"matter": ["This field is required."]})
        response = self.api("post", "/api/v1/notices/", {"kind": "rfe", "matter": "nope"})
        self.assertEqual(fields(response), {"matter": ["No matter has that ID."]})
        response = self.api(
            "post",
            "/api/v1/notices/",
            {
                "matter": str(self.matter.pk),
                "kind": "rfe",
                "receipt_number": "ioe-0912345678",
                "response_due_date": "2026-06-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        notice = response.json()
        self.assertEqual(notice["receipt_number"], RECEIPT)
        self.assertEqual(notice["notice_date"], timezone.localdate().isoformat())
        deadline = WorkItem.objects.get(pk=notice["deadline"])
        self.assertEqual(deadline.priority, WorkItem.Priority.HIGH)
        response = self.api(
            "patch", f"/api/v1/notices/{notice['id']}/", {"response_due_date": "2026-06-15"}
        )
        self.assertEqual(response.json()["response_due_date"], "2026-06-15")
        deadline.refresh_from_db()
        self.assertEqual(timezone.localtime(deadline.scheduled_for).date(), date(2026, 6, 15))
        response = self.api("patch", f"/api/v1/notices/{notice['id']}/", {"receipt_number": "x"})
        self.assertIn("receipt_number", fields(response))
        listing = self.api("get", "/api/v1/notices/?receipt_number=ioe0912345678&open_responses=1")
        self.assertEqual(len(listing.json()["results"]), 1)
        self.assertEqual(CaseNotice.objects.count(), 1)

    def test_status_checks(self):
        payload = {
            "matter": str(self.matter.pk),
            "receipt_number": RECEIPT,
            "status_title": "Case Was Received",
            "checked_at": "2026-03-05T09:00:00Z",
        }
        first = self.api("post", "/api/v1/status-checks/", payload)
        self.assertEqual((first.status_code, first.json()["changed"]), (201, False))
        changed = self.api(
            "post", "/api/v1/status-checks/", {**payload, "status_title": "Case Was Approved"}
        )
        self.assertTrue(changed.json()["changed"])
        response = self.api("post", "/api/v1/status-checks/", {**payload, "receipt_number": "x"})
        self.assertIn("receipt_number", fields(response))
        listing = self.api("get", f"/api/v1/status-checks/?matter={self.matter.pk}")
        self.assertEqual(len(listing.json()["results"]), 2)


class InvoiceEndpointTests(ApiTestCase):
    def test_invoices_are_read_only_with_money_as_strings(self):
        contact = make_contact(self.user)
        invoice = Invoice.objects.create(contact=contact, created_by=self.user)
        add_line(invoice, description="Fictional legal fee", unit_price=Decimal("1500"))
        add_line(
            invoice, description="Fictional advance", unit_price=Decimal("500"), funds="advance"
        )
        issue_invoice(invoice, self.user)
        record_payment(
            invoice,
            self.user,
            amount="250",
            method="check",
            deposit_account="operating",
            received_on=date(2026, 3, 2),
        )
        listing = self.api("get", "/api/v1/invoices/?status=partially_paid").json()["results"]
        self.assertEqual(len(listing), 1)
        body = self.api("get", f"/api/v1/invoices/{invoice.pk}/").json()
        self.assertEqual(
            (body["subtotal"], body["earned_subtotal"], body["advance_subtotal"]),
            ("2000.00", "1500.00", "500.00"),
        )
        self.assertEqual((body["amount_paid"], body["balance"]), ("250.00", "1750.00"))
        self.assertEqual(body["lines"][1]["funds"], "advance")
        self.assertEqual(body["payments"][0]["amount"], "250.00")
        self.assertEqual(self.api("post", "/api/v1/invoices/", {}).status_code, 405)


class UserEndpointTests(ApiTestCase):
    def test_lists_active_staff(self):
        make_user("paralegal", superuser=False)
        inactive = make_user("former", superuser=False)
        inactive.is_active = False
        inactive.save()
        names = [u["username"] for u in self.api("get", "/api/v1/users/").json()["results"]]
        self.assertEqual(names, ["attorney", "paralegal"])
