from unittest import mock

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from crm import leads
from crm.connect.models import Connection
from crm.connect.records import (
    CalendarEventRecord,
    CaseStatusRecord,
    LeadRecord,
    PaymentRecord,
    TextRecord,
)
from crm.connect.tasks import store_records
from crm.models import Activity, Contact
from crm.tests.factories import make_contact, make_matter


class RecordRecoveryTests(TestCase):
    def setUp(self):
        self.connection = Connection.objects.create(connector_key="example", label="Fictional")
        self.connector = self.connection.connector()

    def store(self, capability, records):
        store_records(self.connector, self.connection, capability, records, "webhook")

    def test_receivers_propagate_storage_failures(self):
        make_matter(make_contact(), receipt_number="IOE0000000001")
        common = {"external_id": "fictional-record", "occurred_at": timezone.now()}
        cases = [
            (
                "texting",
                TextRecord(**common, direction="inbound", remote="+12025550143"),
                "crm.comms.records.store_one",
            ),
            ("payments", PaymentRecord(**common, amount="100.00"), "crm.billing.records.store_one"),
            (
                "calendar",
                CalendarEventRecord(**common, title="Fictional consult"),
                "crm.appointments.store_event",
            ),
            (
                "lead_capture",
                LeadRecord(**common, name="Fictional Lead", email="lead@example.test"),
                "crm.leads.store_lead",
            ),
            (
                "case_status",
                CaseStatusRecord(
                    **common, receipt_number="IOE0000000001", status_title="Case Received"
                ),
                "crm.notices.records.store_reading",
            ),
        ]
        for capability, record, target in cases:
            with self.subTest(capability=capability):
                with mock.patch(target, side_effect=OperationalError("Fictional failure")) as save:
                    with self.assertRaises(OperationalError):
                        self.store(capability, [record])
                    save.assert_called_once()

    def test_partial_lead_batch_rolls_back_and_can_be_retried_without_duplicate_notes(self):
        records = [
            LeadRecord(
                external_id=f"lead-{n}",
                occurred_at=timezone.now(),
                name=f"Fictional Lead {n}",
                email=f"lead{n}@example.test",
            )
            for n in (1, 2)
        ]
        original = leads.store_lead

        def fail_after_save(connection, record):
            result = original(connection, record)
            if record.external_id == "lead-2":
                raise OperationalError("Fictional failure")
            return result

        with mock.patch.object(leads, "store_lead", side_effect=fail_after_save):
            with self.assertRaises(OperationalError):
                self.store("lead_capture", records)
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(Activity.objects.exists())
        self.store("lead_capture", records)
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(Activity.objects.filter(kind=Activity.Kind.INTAKE).count(), 2)
