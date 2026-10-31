import json
from datetime import UTC, date, datetime
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.test import TestCase

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, SyncRun
from crm.connect.records import ShipmentRecord
from crm.connect.tasks import run_sync
from crm.connect.testing import ConnectorContract
from crm.models import Activity
from crm.shipments.models import Shipment
from crm.tests.factories import make_contact, make_matter, make_user

from . import BATCH_SIZE, UspsConnector

CREDENTIALS = {"client_id": "fictional-consumer-key", "client_secret": "fictional-secret"}


def number(n):
    return f"94001000000000000000{n:02d}"


def event(code, kind, when="2026-10-27T09:42:18-05:00", gmt="2026-10-27T14:42:18.000Z"):
    return {
        "eventType": kind,
        "eventCode": code,
        "eventTimestamp": when,
        "GMTTimestamp": gmt,
        "eventCity": "CHICAGO",
        "eventState": "IL",
        "eventZIPCode": "60680",
    }


# Fictional packages, in the shape of USPS's Tracking v3r2 reference.
PACKAGES = {
    number(1): {
        "status": "Delivered, PO Box",
        "statusCategory": "Delivered",
        "statusSummary": "Your item has been delivered and is available at a PO Box in "
        "CHICAGO, IL 60680.",
        "trackingEvents": [event("01", "Delivered, PO Box"), event("07", "Arrived at Post Office")],
    },
    number(2): {
        "status": "Arrived at USPS Facility",
        "statusCategory": "On the Way",
        "statusSummary": "Your item arrived at our CHICAGO IL DISTRIBUTION CENTER.",
        "deliveryDateExpectation": {"expectedDeliveryDate": "2026-11-02"},
        "trackingEvents": [event("10", "Arrived at USPS Facility")],
    },
    number(3): {"statusCategory": "Out for Delivery", "status": "Out for Delivery"},
    number(4): {
        "statusCategory": "Alert",
        "status": "Insufficient Address",
        "trackingEvents": [event("22", "Insufficient Address")],
    },
    number(5): {
        "statusCategory": "Delivered",
        "status": "Delivered, To Original Sender",
        "trackingEvents": [event("01", "Delivered, To Original Sender")],
    },
    number(6): {"statusCategory": "Delivery Attempt: Action Needed", "status": "Notice Left"},
    number(9): {"statusCategory": "USPS Awaiting Item", "status": "Label Created"},
    number(10): {"statusCategory": "Moving Through Network", "status": "In Transit"},
}
ERRORS = {
    number(7): {"statusCode": "404", "error": {"code": "404", "message": "Not Found"}},
    number(8): {
        "statusCode": "403",
        "error": {"code": "403", "message": "Not authorized for this Mailer ID"},
    },
}


class FakeUsps:
    def __init__(self):
        self.requests = []
        self.tokens = 0
        self.expire_next = False
        self.refuse = False

    def tracking_calls(self):
        return [r for r in self.requests if r.url.path == "/tracking/v3r2/tracking"]

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "apis.usps.com"
        if request.url.path == "/oauth2/v3/token":
            form = parse_qs(request.content.decode())
            if form != {
                "grant_type": ["client_credentials"],
                "client_id": [CREDENTIALS["client_id"]],
                "client_secret": [CREDENTIALS["client_secret"]],
            }:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": 28799}
            )
        assert request.url.path == "/tracking/v3r2/tracking"
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"error": {"message": "expired"}})
        if self.refuse:
            return httpx.Response(403, json={"error": {"message": "forbidden"}})
        assert request.headers["authorization"].startswith("Bearer token-")
        asked = [item["trackingNumber"] for item in json.loads(request.content)]
        assert len(asked) <= BATCH_SIZE
        items = []
        for tracking in asked:
            if tracking in ERRORS:
                items.append({"trackingNumber": tracking, **ERRORS[tracking]})
            else:
                found = PACKAGES.get(tracking, {"statusCategory": "On the Way"})
                items.append({"trackingNumber": tracking, "statusCode": "200", **found})
        return httpx.Response(207 if set(asked) & set(ERRORS) else 200, json=items)


class FakeUspsMixin:
    def setUp(self):
        super().setUp()
        self.usps = FakeUsps()
        self.enterContext(http.use_transport(httpx.MockTransport(self.usps)))
        # Retries wait between attempts; the tests need not.
        self.enterContext(mock.patch.object(http, "sleep"))
        self.admin = make_user()
        self.matter = make_matter(make_contact(self.admin), self.admin)

    def ship(self, tracking, **extra):
        return Shipment.objects.create(
            matter=self.matter, description="Filing package", tracking_number=tracking, **extra
        )


class UspsContractTests(FakeUspsMixin, ConnectorContract, TestCase):
    connector_class = UspsConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "client_secret": "wrong"}
    sync_examples = {"shipping": {}}

    def setUp(self):
        super().setUp()
        self.ship(number(1))


class UspsTests(FakeUspsMixin, TestCase):
    def connector(self, credentials=CREDENTIALS):
        return UspsConnector(Connection(connector_key="usps", credentials=credentials))

    def test_each_usps_status_in_the_crms_terms(self):
        for n in range(1, 11):
            self.ship(number(n))
        self.ship("1Z0000000000000001", carrier="ups")
        batch = self.connector().sync("shipping", {})
        found = {record.tracking_number: record for record in batch.records}
        self.assertEqual(len(found), 10)  # The UPS package is not USPS's to answer.
        statuses = {n: (found[n].status or found[n].problem) for n in found}
        self.assertEqual(
            statuses,
            {
                number(1): "delivered",
                number(2): "in_transit",
                number(3): "out_for_delivery",
                number(4): "returned",
                number(5): "returned",
                number(6): "problem",
                number(7): "not_found",
                number(8): "not_shared",
                number(9): "sent",
                number(10): "in_transit",
            },
        )
        delivered = found[number(1)]
        self.assertEqual(delivered.delivered_at, datetime(2026, 10, 27, 14, 42, 18, tzinfo=UTC))
        self.assertTrue(delivered.detail.startswith("Your item has been delivered"))
        self.assertEqual(found[number(2)].expected_on, date(2026, 11, 2))
        for record in batch.records:
            self.assertIsInstance(record, ShipmentRecord)
            self.assertEqual(record.problems(), [])

    def test_numbers_go_35_to_a_request_with_one_token(self):
        for n in range(BATCH_SIZE + 5):
            self.ship(f"9400200000000000{n:06d}")
        self.connector().sync("shipping", {})
        self.assertEqual(
            [len(json.loads(r.content)) for r in self.usps.tracking_calls()], [BATCH_SIZE, 5]
        )
        self.assertEqual(self.usps.tokens, 1)

    def test_an_expired_token_is_renewed_once(self):
        self.ship(number(2))
        self.usps.expire_next = True
        batch = self.connector().sync("shipping", {})
        self.assertEqual((len(batch.records), self.usps.tokens), (1, 2))

    def test_refusals_explain_what_to_do(self):
        with self.assertRaisesMessage(ConnectorError, "did not accept the consumer key"):
            self.connector({**CREDENTIALS, "client_id": "wrong"}).test_connection()
        self.ship(number(2))
        self.usps.refuse = True
        with self.assertRaisesMessage(ConnectorError, "has the Tracking API"):
            self.connector().sync("shipping", {})
        with self.assertRaisesMessage(ConnectorError, "does not sync"):
            self.connector().sync("texting", {})

    def test_odd_answers_are_taken_as_far_as_they_go(self):
        def answer(token=None, tracking=None, status=200):
            def handler(request):
                if request.url.path == "/oauth2/v3/token":
                    return httpx.Response(status, json=token or {})
                return httpx.Response(200, **tracking)

            return http.use_transport(httpx.MockTransport(handler))

        self.ship(number(11))
        odd = {
            "trackingNumber": number(11),
            "statusCategory": "Delivered",
            "deliveryDateExpectation": {"expectedDeliveryDate": "soon"},
            "trackingEvents": [{"eventCode": "01", "GMTTimestamp": "late", "eventTimestamp": ""}],
        }
        with answer({"access_token": "t", "expires_in": "never"}, {"json": [odd]}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(
            (record.status, record.delivered_at, record.expected_on), ("delivered", None, None)
        )
        with answer(
            {"access_token": "t"}, {"json": [{"trackingNumber": number(11), "statusCode": "500"}]}
        ):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(record.problem, "unavailable")
        with answer({"access_token": "t"}, {"content": b"not json"}):
            with self.assertRaisesMessage(ConnectorError, "not understood"):
                self.connector().sync("shipping", {})
        with answer({}), self.assertRaisesMessage(ConnectorError, "did not return an access token"):
            self.connector().test_connection()
        with answer(status=500), self.assertRaises(ConnectorError):
            self.connector().test_connection()

    def test_the_worker_tracks_shipments_and_notes_their_delivery(self):
        delivered = self.ship(number(1), sent_to="USCIS Chicago lockbox")
        held = self.ship(number(8))
        connection = Connection.objects.create(
            connector_key="usps",
            label="USPS",
            enabled_capabilities=["shipping"],
            credentials=CREDENTIALS,
        )
        run = SyncRun.objects.create(connection=connection, capability="shipping")
        run_sync.call(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.records_received), ("succeeded", 2))
        delivered.refresh_from_db()
        held.refresh_from_db()
        self.assertEqual(delivered.status, "delivered")
        self.assertFalse(held.track)
        note = Activity.objects.get(matter=self.matter)
        self.assertIn("was delivered to USCIS Chicago lockbox", note.body)
