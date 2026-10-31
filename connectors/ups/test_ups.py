import base64
from datetime import UTC, date, datetime
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.test import TestCase

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.records import ShipmentRecord
from crm.connect.testing import ConnectorContract
from crm.shipments.models import Shipment
from crm.tests.factories import make_contact, make_matter, make_user

from . import PER_RUN, UpsConnector

CREDENTIALS = {"client_id": "fictional-client-id", "client_secret": "fictional-secret"}
BASIC = "Basic " + base64.b64encode(b"fictional-client-id:fictional-secret").decode()


def package(kind, code, description, city="CHICAGO", **extra):
    status = {"type": kind, "code": "XX", "description": description, "statusCode": code}
    return {
        "currentStatus": status,
        "activity": [
            {
                "location": {"address": {"city": city, "stateProvince": "IL", "countryCode": "US"}},
                "status": status,
                "date": "20261028",
                "time": "143200",
                "gmtDate": "20261028",
                "gmtTime": "193200",
                "gmtOffset": "-05:00",
            }
        ],
        **extra,
    }


# Fictional packages, in the shape of UPS's Tracking API reference.
PACKAGES = {
    "1Z999AA10000000001": package(
        "D", "011", "DELIVERED", deliveryDate=[{"type": "DEL", "date": "20261028"}]
    ),
    "1Z999AA10000000002": package(
        "I",
        "005",
        "On the Way",
        city="HODGKINS",
        deliveryDate=[{"type": "SDD", "date": "20261103"}],
    ),
    "1Z999AA10000000003": package("I", "021", "Out For Delivery Today"),
    "1Z999AA10000000004": package("X", "013", "Exception: incorrect address"),
    "1Z999AA10000000005": package("I", "034", "Returned to Sender"),
    "1Z999AA10000000006": package("M", "003", "Shipper created a label"),
    "1Z999AA10000000009": package(
        "I",
        "005",
        "On the Way",
        deliveryDate=[{"type": "SDD", "date": "soon"}, {"type": "RDD", "date": "20261105"}],
    ),
}


class FakeUps:
    def __init__(self):
        self.requests = []
        self.tokens = 0
        self.expire_next = False
        self.refuse = False

    def tracking_calls(self):
        return [r for r in self.requests if r.url.path.startswith("/api/track/")]

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "onlinetools.ups.com"
        if request.url.path == "/security/v1/oauth/token":
            if request.headers.get("authorization") != BASIC or parse_qs(
                request.content.decode()
            ) != {"grant_type": ["client_credentials"]}:
                return httpx.Response(401, json={"response": {"errors": [{"code": "10401"}]}})
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": "14399"}
            )
        assert request.url.path.startswith("/api/track/v1/details/")
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"response": {"errors": [{"code": "251004"}]}})
        if self.refuse:
            return httpx.Response(403, json={"response": {"errors": [{"code": "10403"}]}})
        assert request.headers["transactionSrc"] == "open-immigration-crm"
        assert len(request.headers["transId"]) == 32
        assert request.url.params["locale"] == "en_US"
        number = request.url.path.rsplit("/", 1)[-1]
        if number == "1Z999AA10000000007":
            return httpx.Response(404, json={"response": {"errors": [{"code": "151044"}]}})
        if number == "1Z999AA10000000008":
            return httpx.Response(500, json={"response": {"errors": [{"code": "10004"}]}})
        if number == "1Z999AA10000000010":
            return httpx.Response(
                200, json={"trackResponse": {"shipment": [{"warnings": [{"code": "TW0001"}]}]}}
            )
        found = PACKAGES.get(number, package("I", "005", "On the Way"))
        return httpx.Response(
            200,
            json={
                "trackResponse": {"shipment": [{"package": [{"trackingNumber": number, **found}]}]}
            },
        )


class FakeUpsMixin:
    def setUp(self):
        super().setUp()
        self.ups = FakeUps()
        self.enterContext(http.use_transport(httpx.MockTransport(self.ups)))
        # Requests are spaced and retried with waits; the tests need not wait.
        self.enterContext(mock.patch.object(http, "sleep"))
        self.admin = make_user()
        self.matter = make_matter(make_contact(self.admin), self.admin)

    def ship(self, tracking, **extra):
        return Shipment.objects.create(
            matter=self.matter,
            description="Filing package",
            carrier=extra.pop("carrier", "ups"),
            tracking_number=tracking,
            **extra,
        )


class UpsContractTests(FakeUpsMixin, ConnectorContract, TestCase):
    connector_class = UpsConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "client_secret": "wrong"}
    sync_examples = {"shipping": {}}

    def setUp(self):
        super().setUp()
        self.ship("1Z999AA10000000001")


class UpsTests(FakeUpsMixin, TestCase):
    def connector(self, credentials=CREDENTIALS):
        return UpsConnector(Connection(connector_key="ups", credentials=credentials))

    def test_each_ups_status_in_the_crms_terms(self):
        for n in range(1, 11):
            self.ship(f"1Z999AA100000000{n:02d}")
        self.ship("794600000001", carrier="fedex")
        batch = self.connector().sync("shipping", {})
        found = {record.tracking_number: record for record in batch.records}
        self.assertEqual(
            {n: (r.status or r.problem) for n, r in found.items()},
            {
                "1Z999AA10000000001": "delivered",
                "1Z999AA10000000002": "in_transit",
                "1Z999AA10000000003": "out_for_delivery",
                "1Z999AA10000000004": "problem",
                "1Z999AA10000000005": "returned",
                "1Z999AA10000000006": "sent",
                "1Z999AA10000000007": "not_found",
                "1Z999AA10000000008": "unavailable",
                "1Z999AA10000000009": "in_transit",
                "1Z999AA10000000010": "not_found",
            },
        )
        delivered = found["1Z999AA10000000001"]
        self.assertEqual(
            (delivered.delivered_at, delivered.detail),
            (datetime(2026, 10, 28, 19, 32, tzinfo=UTC), "DELIVERED, CHICAGO, IL"),
        )
        self.assertEqual(found["1Z999AA10000000002"].expected_on, date(2026, 11, 3))
        self.assertEqual(found["1Z999AA10000000009"].expected_on, date(2026, 11, 5))
        for record in batch.records:
            self.assertIsInstance(record, ShipmentRecord)
            self.assertEqual(record.problems(), [])
        self.assertEqual(self.ups.tokens, 1)

    def test_a_run_checks_at_most_40_numbers(self):
        for n in range(PER_RUN + 3):
            self.ship(f"1Z999AA20000{n:06d}")
        self.connector().sync("shipping", {})
        self.assertEqual(len(self.ups.tracking_calls()), PER_RUN)

    def test_an_expired_token_is_renewed_once(self):
        self.ship("1Z999AA10000000002")
        self.ups.expire_next = True
        batch = self.connector().sync("shipping", {})
        self.assertEqual((len(batch.records), self.ups.tokens), (1, 2))

    def test_refusals_and_odd_answers(self):
        with self.assertRaisesMessage(ConnectorError, "did not accept the client ID"):
            self.connector({**CREDENTIALS, "client_id": "wrong"}).test_connection()
        self.ship("1Z999AA10000000002")
        self.ups.refuse = True
        with self.assertRaisesMessage(ConnectorError, "includes the Tracking API"):
            self.connector().sync("shipping", {})
        with self.assertRaisesMessage(ConnectorError, "does not sync"):
            self.connector().sync("texting", {})

        def answer(token=None, tracking=None, status=200):
            def handler(request):
                if request.url.path == "/security/v1/oauth/token":
                    return httpx.Response(status, json=token or {})
                return httpx.Response(200, **tracking)

            return http.use_transport(httpx.MockTransport(handler))

        odd = package("D", "011", "DELIVERED")
        odd["activity"][0]["gmtTime"] = "late"
        body = {"trackResponse": {"shipment": [{"package": [odd]}]}}
        with answer({"access_token": "t", "expires_in": "never"}, {"json": body}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual((record.status, record.delivered_at), ("delivered", None))
        # A code that is not a number, a date that is not one, and no delivery scan.
        stranger = package("D", "n/a", "DELIVERED", deliveryDate=[{"type": "SDD", "date": "soon"}])
        stranger["activity"][0]["status"] = {"type": "I"}
        body = {"trackResponse": {"shipment": [{"package": [stranger]}]}}
        with answer({"access_token": "t"}, {"json": body}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(
            (record.status, record.delivered_at, record.expected_on), ("delivered", None, None)
        )
        with answer({"access_token": "t"}, {"content": b"not json"}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(record.problem, "unavailable")
        with answer({}), self.assertRaisesMessage(ConnectorError, "did not return an access token"):
            self.connector().test_connection()
        with answer(status=500), self.assertRaises(ConnectorError):
            self.connector().test_connection()
