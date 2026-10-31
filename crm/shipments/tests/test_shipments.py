from datetime import UTC, date, datetime, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect import lookups
from crm.connect.records import ShipmentRecord
from crm.connect.signals import records_received
from crm.models import Activity, AuditLog, WorkItem
from crm.shipments.models import Shipment
from crm.shipments.services import PROBLEMS, shipments_to_check
from crm.tests.factories import make_contact, make_matter, make_user

NUMBER = "9400100000000000000001"


def reading(number=NUMBER, carrier="usps", **extra):
    return ShipmentRecord(
        external_id=f"{number}@1",
        occurred_at=timezone.now(),
        carrier=carrier,
        tracking_number=number,
        **({"status": "in_transit"} if "status" not in extra and "problem" not in extra else {}),
        **extra,
    )


class ShipmentCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.client.force_login(self.admin)
        self.matter = make_matter(make_contact(self.admin), self.admin)

    def ship(self, number=NUMBER, **extra):
        return Shipment.objects.create(
            matter=self.matter, description="I-130 package", tracking_number=number, **extra
        )

    def report(self, *records):
        records_received.send(
            sender=None,
            connection=None,
            capability="shipping",
            records=list(records),
            source="sync",
        )


class ManualShipmentTests(ShipmentCase):
    def test_staff_add_mark_delivered_and_delete_a_shipment(self):
        url = reverse("matter_detail", args=[self.matter.pk])
        page = self.client.get(url)
        self.assertContains(page, "No shipments recorded.")
        self.assertContains(page, "to track packages automatically")
        self.client.post(
            reverse("shipment_create", args=[self.matter.pk]),
            {
                "description": "I-130 package",
                "carrier": "usps",
                "tracking_number": "9400 1000 0000-0000 0000 01",
                "sent_to": "USCIS Chicago lockbox",
                "sent_on": "2026-10-20",
            },
        )
        shipment = Shipment.objects.get()
        self.assertEqual(
            (shipment.tracking_number, shipment.status, shipment.created_by),
            ("9400100000000000000001", "sent", self.admin),
        )
        self.assertContains(self.client.get(url), "USCIS Chicago lockbox")
        answer = self.client.post(
            reverse("shipment_create", args=[self.matter.pk]), {}, follow=True
        )
        self.assertContains(answer, "Fill in what was sent and when.")

        edit = reverse("shipment_update", args=[shipment.pk])
        self.assertContains(self.client.get(edit), "Delivered on")
        fields = {
            "description": "I-130 package",
            "carrier": "usps",
            "tracking_number": shipment.tracking_number,
            "sent_on": "2026-10-20",
        }
        self.client.post(edit, {**fields, "status": "delivered", "delivered_on": "2026-10-23"})
        shipment.refresh_from_db()
        self.assertEqual(timezone.localdate(shipment.delivered_at), date(2026, 10, 23))
        self.assertTrue(shipment.settled)
        self.client.post(edit, {**fields, "status": "delivered"})  # The date it has is kept.
        shipment.refresh_from_db()
        self.assertEqual(timezone.localdate(shipment.delivered_at), date(2026, 10, 23))
        self.client.post(edit, {**fields, "status": "in_transit"})
        shipment.refresh_from_db()
        self.assertIsNone(shipment.delivered_at)
        self.client.post(edit, {**fields, "status": "delivered"})
        shipment.refresh_from_db()
        self.assertEqual(timezone.localdate(shipment.delivered_at), timezone.localdate())

        self.client.post(reverse("shipment_delete", args=[shipment.pk]))
        self.assertFalse(Shipment.objects.exists())
        self.assertEqual(
            sorted(
                AuditLog.objects.filter(action__startswith="shipment.").values_list(
                    "action", flat=True
                )
            ),
            [
                "shipment.created",
                "shipment.deleted",
                "shipment.updated",
                "shipment.updated",
                "shipment.updated",
                "shipment.updated",
            ],
        )


class TrackingTests(ShipmentCase):
    def test_which_shipments_are_due_for_a_check(self):
        now = timezone.now()
        never = self.ship("A1", sent_on=timezone.localdate() - timedelta(days=3))
        self.ship("A1")  # The same number twice is asked about once.
        stale = self.ship("B2", checked_at=now - timedelta(hours=5))
        self.ship("C3", checked_at=now - timedelta(hours=1))
        self.ship("D4", status=Shipment.Status.DELIVERED)
        self.ship("E5", carrier="ups")
        self.ship("F6", sent_on=timezone.localdate() - timedelta(days=61))
        self.ship("")
        self.assertEqual(shipments_to_check(limit=10, carrier="usps"), ["A1", "B2"])
        self.assertEqual(lookups.due("shipping", limit=1, carrier="usps"), ["A1"])
        self.assertEqual(lookups.due("shipping", limit=10, carrier="ups"), ["E5"])
        self.assertEqual(sorted(shipments_to_check(limit=10)), ["A1", "B2", "E5"])
        self.assertTrue(never and stale)

    def test_readings_update_the_shipment_and_note_what_matters(self):
        shipment = self.ship(sent_to="USCIS Chicago lockbox")
        self.report(reading(detail="In transit to next facility", expected_on=date(2026, 10, 25)))
        shipment.refresh_from_db()
        self.assertEqual(
            (shipment.status, shipment.status_detail, shipment.expected_on),
            ("in_transit", "In transit to next facility", date(2026, 10, 25)),
        )
        self.report(reading(detail="Arrived at facility"), object())
        shipment.refresh_from_db()
        self.assertEqual(shipment.status_detail, "Arrived at facility")
        self.assertFalse(Activity.objects.exists())  # In transit is not news.

        self.report(reading(problem="not_found"))
        shipment.refresh_from_db()
        self.assertEqual(
            (shipment.status, shipment.tracking_problem), ("in_transit", PROBLEMS["not_found"])
        )
        self.report(reading(problem="unavailable"))
        shipment.refresh_from_db()
        self.assertIn("just now", shipment.tracking_problem)
        self.assertEqual(
            shipments_to_check(limit=5, now=timezone.now() + timedelta(hours=5)), [NUMBER]
        )

        delivered = datetime(2026, 10, 24, 15, 2, tzinfo=UTC)
        record = reading(
            status="delivered",
            detail="Delivered, In/At Mailbox, CHICAGO, IL",
            delivered_at=delivered,
        )
        self.report(record, record)
        shipment.refresh_from_db()
        self.assertEqual(
            (shipment.status, shipment.delivered_at, shipment.tracking_problem),
            ("delivered", delivered, ""),
        )
        [note] = Activity.objects.filter(matter=self.matter)
        self.assertEqual(note.subject, "Delivered: I-130 package")
        self.assertIn("was delivered to USCIS Chicago lockbox on Oct 24, 2026", note.body)
        self.assertEqual(shipments_to_check(limit=5), [])
        self.report(reading(status="returned"))  # A settled shipment is left alone.
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, "delivered")

    def test_a_package_the_carrier_will_not_share_is_no_longer_checked(self):
        shipment = self.ship()
        self.report(reading(problem="not_shared"))
        shipment.refresh_from_db()
        self.assertFalse(shipment.track)
        self.assertIn("Update it by hand", shipment.tracking_problem)
        later = timezone.now() + timedelta(days=1)
        self.assertEqual(shipments_to_check(limit=5, now=later), [])
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "would not share this package")
        self.assertContains(
            self.client.get(reverse("shipment_update", args=[shipment.pk])),
            "Check its tracking automatically",
        )

    def test_a_problem_gets_a_task(self):
        self.ship()
        self.report(
            reading(status="problem", detail="Alert: Insufficient address"),
            reading("UNKNOWN0001", status="delivered"),
            reading(carrier="ups"),
        )
        note = Activity.objects.get()
        self.assertEqual(note.subject, "Needs attention: I-130 package")
        task = WorkItem.objects.get()
        self.assertEqual(
            (task.title, task.priority, task.assigned_to, task.matter),
            ("Check on shipment: I-130 package", "high", self.admin, self.matter),
        )
        self.assertIn("Alert: Insufficient address", task.description)
        records_received.send(
            sender=None, connection=None, capability="texting", records=[reading()], source="sync"
        )

    def test_shipment_records_check_themselves(self):
        self.assertEqual(reading().problems(), [])
        broken = ShipmentRecord(
            external_id="x",
            occurred_at=timezone.now(),
            carrier="",
            tracking_number="",
            status="lost",
            problem="gone",
            delivered_at=datetime(2026, 1, 1),
            expected_on="soon",
        )
        self.assertEqual(len(broken.problems()), 6)
        self.assertEqual(str(self.ship()), "I-130 package")
