import json
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

from . import BATCH_SIZE, FedexConnector

CREDENTIALS = {"client_id": "fictional-api-key", "client_secret": "fictional-secret-key"}


def status(code, description, city="CHICAGO", **extra):
    return {
        "latestStatusDetail": {
            "code": code,
            "derivedCode": code,
            "description": description,
            "scanLocation": {"city": city, "stateOrProvinceCode": "IL"},
            **extra,
        }
    }


# Fictional packages, in the shape of FedEx's Basic Integrated Visibility reference.
PACKAGES = {
    "794600000001": {
        **status("DL", "Delivered"),
        "dateAndTimes": [
            {"type": "ACTUAL_DELIVERY", "dateTime": "2026-10-28T10:14:00-05:00"},
            {"type": "SHIP", "dateTime": "2026-10-27T00:00:00-06:00"},
        ],
    },
    "794600000002": {
        **status("IT", "In transit", city="MEMPHIS"),
        "dateAndTimes": [{"type": "ESTIMATED_DELIVERY", "dateTime": "2026-11-03T20:00:00-05:00"}],
    },
    "794600000003": status("OD", "On FedEx vehicle for delivery"),
    "794600000004": status(
        "DE",
        "Delivery exception",
        ancillaryDetails=[{"reasonDescription": "Incorrect address"}],
    ),
    "794600000005": status("XX", "Returning package to shipper"),
    "794600000006": status("OC", "Shipment information sent to FedEx"),
    "794600000009": {
        **status("AR", "Arrived at FedEx location"),
        "estimatedDeliveryTimeWindow": {"window": {"ends": "2026-11-04T18:00:00-05:00"}},
    },
}
ERRORS = {
    "794600000007": {"code": "TRACKING.TRACKINGNUMBER.NOTFOUND", "message": "Not found"},
    "794600000008": {"code": "TRACKING.AUTHENTICATEDDELIVERY.ERROR", "message": "Restricted"},
    "794600000010": {"code": "SYSTEM.UNAVAILABLE.EXCEPTION", "message": "Try later"},
}


class FakeFedex:
    def __init__(self):
        self.requests = []
        self.tokens = 0
        self.expire_next = False
        self.refuse = False

    def tracking_calls(self):
        return [r for r in self.requests if r.url.path == "/track/v1/trackingnumbers"]

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "apis.fedex.com"
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form != {
                "grant_type": ["client_credentials"],
                "client_id": [CREDENTIALS["client_id"]],
                "client_secret": [CREDENTIALS["client_secret"]],
            }:
                return httpx.Response(401, json={"errors": [{"code": "NOT.AUTHORIZED.ERROR"}]})
            self.tokens += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.tokens}",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/track/v1/trackingnumbers"
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"errors": [{"code": "NOT.AUTHORIZED.ERROR"}]})
        if self.refuse:
            return httpx.Response(403, json={"errors": [{"code": "FORBIDDEN.ERROR"}]})
        assert request.headers["x-locale"] == "en_US"
        body = json.loads(request.content)
        assert body["includeDetailedScans"] is False
        asked = [item["trackingNumberInfo"]["trackingNumber"] for item in body["trackingInfo"]]
        assert len(asked) <= BATCH_SIZE
        results = []
        for number in asked:
            if number in ERRORS:
                result = {"trackingNumberInfo": {"trackingNumber": number}, "error": ERRORS[number]}
            else:
                result = PACKAGES.get(number, status("IT", "In transit"))
            results.append({"trackingNumber": number, "trackResults": [result]})
        return httpx.Response(200, json={"output": {"completeTrackResults": results}})


class FakeFedexMixin:
    def setUp(self):
        super().setUp()
        self.fedex = FakeFedex()
        self.enterContext(http.use_transport(httpx.MockTransport(self.fedex)))
        # Retries wait between attempts; the tests need not.
        self.enterContext(mock.patch.object(http, "sleep"))
        self.admin = make_user()
        self.matter = make_matter(make_contact(self.admin), self.admin)

    def ship(self, tracking, **extra):
        return Shipment.objects.create(
            matter=self.matter,
            description="Filing package",
            carrier=extra.pop("carrier", "fedex"),
            tracking_number=tracking,
            **extra,
        )


class FedexContractTests(FakeFedexMixin, ConnectorContract, TestCase):
    connector_class = FedexConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "client_secret": "wrong"}
    sync_examples = {"shipping": {}}

    def setUp(self):
        super().setUp()
        self.ship("794600000001")


class FedexTests(FakeFedexMixin, TestCase):
    def connector(self, credentials=CREDENTIALS):
        return FedexConnector(Connection(connector_key="fedex", credentials=credentials))

    def test_each_fedex_status_in_the_crms_terms(self):
        for n in range(1, 11):
            self.ship(f"7946000000{n:02d}")
        self.ship("9400100000000000000001", carrier="usps")
        batch = self.connector().sync("shipping", {})
        found = {record.tracking_number: record for record in batch.records}
        self.assertEqual(
            {n: (r.status or r.problem) for n, r in found.items()},
            {
                "794600000001": "delivered",
                "794600000002": "in_transit",
                "794600000003": "out_for_delivery",
                "794600000004": "problem",
                "794600000005": "returned",
                "794600000006": "sent",
                "794600000007": "not_found",
                "794600000008": "not_shared",
                "794600000009": "in_transit",
                "794600000010": "unavailable",
            },
        )
        delivered = found["794600000001"]
        self.assertEqual(
            (delivered.delivered_at, delivered.detail),
            (datetime(2026, 10, 28, 15, 14, tzinfo=UTC), "Delivered, CHICAGO, IL"),
        )
        self.assertEqual(found["794600000002"].expected_on, date(2026, 11, 3))
        self.assertEqual(found["794600000009"].expected_on, date(2026, 11, 4))
        self.assertEqual(
            found["794600000004"].detail, "Delivery exception, CHICAGO, IL. Incorrect address"
        )
        for record in batch.records:
            self.assertIsInstance(record, ShipmentRecord)
            self.assertEqual(record.problems(), [])

    def test_numbers_go_30_to_a_request_with_one_token(self):
        for n in range(BATCH_SIZE + 2):
            self.ship(f"79470000{n:04d}")
        self.connector().sync("shipping", {})
        self.assertEqual(
            [len(json.loads(r.content)["trackingInfo"]) for r in self.fedex.tracking_calls()],
            [BATCH_SIZE, 2],
        )
        self.assertEqual(self.fedex.tokens, 1)

    def test_an_expired_token_is_renewed_once(self):
        self.ship("794600000002")
        self.fedex.expire_next = True
        batch = self.connector().sync("shipping", {})
        self.assertEqual((len(batch.records), self.fedex.tokens), (1, 2))

    def test_refusals_and_odd_answers(self):
        with self.assertRaisesMessage(ConnectorError, "did not accept the API key"):
            self.connector({**CREDENTIALS, "client_id": "wrong"}).test_connection()
        self.ship("794600000002")
        self.fedex.refuse = True
        with self.assertRaisesMessage(ConnectorError, "includes Basic Integrated Visibility"):
            self.connector().sync("shipping", {})
        with self.assertRaisesMessage(ConnectorError, "does not sync"):
            self.connector().sync("texting", {})

        def answer(token=None, tracking=None, status=200):
            def handler(request):
                if request.url.path == "/oauth/token":
                    return httpx.Response(status, json=token or {})
                return httpx.Response(200, **tracking)

            return http.use_transport(httpx.MockTransport(handler))

        odd = {
            "output": {
                "completeTrackResults": [
                    {
                        "trackingNumber": "794600000002",
                        "trackResults": [
                            {
                                **status("DL", "Delivered"),
                                "dateAndTimes": [
                                    {"type": "ACTUAL_DELIVERY", "dateTime": "late"},
                                    {"type": "ESTIMATED_DELIVERY", "dateTime": "soon"},
                                ],
                            }
                        ],
                    },
                    {"trackingNumber": "794600000099", "trackResults": []},
                ]
            }
        }
        with answer({"access_token": "t", "expires_in": "never"}, {"json": odd}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(
            (record.status, record.delivered_at, record.expected_on), ("delivered", None, None)
        )
        with answer({"access_token": "t"}, {"json": {"output": {}}}):
            [record] = self.connector().sync("shipping", {}).records
        self.assertEqual(record.problem, "unavailable")
        with answer({"access_token": "t"}, {"content": b"not json"}):
            with self.assertRaisesMessage(ConnectorError, "not understood"):
                self.connector().sync("shipping", {})
        with answer({}), self.assertRaisesMessage(ConnectorError, "did not return an access token"):
            self.connector().test_connection()
        with answer(status=500), self.assertRaises(ConnectorError):
            self.connector().test_connection()
