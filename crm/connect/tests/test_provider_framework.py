import importlib
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest import mock

import httpx
from django.apps import apps
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.comms.models import Communication
from crm.connect import http, oauth
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ConnectorApp
from crm.connect.records import (
    RECORD_TYPES,
    BookingRecord,
    CalendarEventRecord,
    CallRecord,
    CaseStatusRecord,
    ContactRecord,
    EmailRecord,
    LeadRecord,
    MatterRecord,
    PaymentRecord,
    SignatureRecord,
    TextRecord,
)
from crm.connect.registry import registry
from crm.matching import counterpart_key, match_contact, match_staff, normalize_phone
from crm.models import Contact
from crm.tests.factories import make_contact, make_user

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
ExampleOAuth = registry.get("example_oauth")


def client(**kwargs):
    return http.ProviderClient("https://api.example.test", name="Example", **kwargs)


class HttpClientTests(TestCase):
    def setUp(self):
        self.waits = []
        patcher = mock.patch.object(http, "sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, handler, call):
        with http.use_transport(httpx.MockTransport(handler)):
            return call()

    def test_json_and_headers(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        result = self.run_with(
            handler, lambda: client(headers={"X-Key": "k"}).get("/items", params={"a": 1})
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(str(seen[0].url), "https://api.example.test/items?a=1")
        self.assertEqual(seen[0].headers["X-Key"], "k")
        self.assertTrue(seen[0].headers["User-Agent"].startswith("OpenImmigrationCRM"))
        empty = self.run_with(lambda r: httpx.Response(204), lambda: client().post("/x"))
        self.assertEqual(empty, {})

    def test_retries_honour_retry_after(self):
        later = format_datetime(timezone.now() + timedelta(seconds=30), usegmt=True)
        answers = iter(
            [
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(503, headers={"Retry-After": later}),
                httpx.Response(500),
                httpx.Response(200, json={"done": 1}),
            ]
        )
        result = self.run_with(lambda r: next(answers), lambda: client().get("/x"))
        self.assertEqual(result, {"done": 1})
        self.assertEqual(self.waits[0], 7)
        self.assertTrue(25 <= self.waits[1] <= 30)
        self.assertEqual(self.waits[2], 4)
        with self.assertRaisesMessage(ConnectorError, "Example answered HTTP 429"):
            self.run_with(
                lambda r: httpx.Response(429, headers={"Retry-After": "9999"}),
                lambda: client(retries=1).get("/x"),
            )
        self.assertEqual(self.waits[-1], http.MAX_RETRY_WAIT)

    def test_failures_become_safe_messages(self):
        with self.assertRaises(ConnectorError) as raised:
            self.run_with(
                lambda r: httpx.Response(404, json={"secret": "x"}), lambda: client().get("/x")
            )
        self.assertEqual(
            (str(raised.exception), raised.exception.status), ("Example answered HTTP 404.", 404)
        )

        def timeout(request):
            raise httpx.ReadTimeout("slow")

        def down(request):
            raise httpx.ConnectError("down")

        for handler, message in (
            (timeout, "did not answer in time"),
            (down, "could not be reached"),
        ):
            with self.assertRaisesMessage(ConnectorError, message):
                self.run_with(handler, lambda: client(retries=0).get("/x"))
        with self.assertRaisesMessage(ConnectorError, "too large"):
            self.run_with(
                lambda r: httpx.Response(200, content=b"x" * 50),
                lambda: client(max_bytes=10).get("/x"),
            )
        with self.assertRaisesMessage(ConnectorError, "not understood"):
            self.run_with(
                lambda r: httpx.Response(200, content=b"<html>"), lambda: client().get("/x")
            )

    def test_requests_can_be_spaced(self):
        spaced = client(min_interval=0.5)
        for _ in range(3):
            self.run_with(lambda r: httpx.Response(204), lambda: spaced.get("/x"))
        self.assertEqual(len(self.waits), 2)
        self.assertTrue(all(0.4 < wait <= 0.5 for wait in self.waits))
        self.run_with(lambda r: httpx.Response(204), lambda: client().get("/x"))
        self.assertEqual(len(self.waits), 2)

    def test_addresses(self):
        with self.assertRaisesMessage(ConnectorError, "HTTPS"):
            http.ProviderClient("http://api.example.test").get("/x")
        local = http.ProviderClient("http://localhost:11434", allow_local_http=True)
        result = self.run_with(
            lambda r: httpx.Response(200, json={}), lambda: local.get("/v1/models")
        )
        self.assertEqual(result, {})
        for host in ("192.168.1.5", "llm.local"):
            http.check_url(f"http://{host}/x", allow_local_http=True)
        with self.assertRaises(ConnectorError):
            http.check_url("http://api.example.test/x", allow_local_http=True)

    def test_paginate(self):
        pages = {
            None: {"items": [1, 2], "next": "b"},
            "b": {"items": [3, 4], "next": "c"},
            "c": {"items": [5], "next": None},
        }

        def handler(request):
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])

        def next_request(page):
            return ("/items", {"cursor": page["next"]}) if page["next"] else None

        with http.use_transport(httpx.MockTransport(handler)):
            items = list(
                client().paginate("/items", items=lambda p: p["items"], next_request=next_request)
            )
            capped = list(
                client().paginate(
                    "/items", items=lambda p: p["items"], next_request=next_request, limit=3
                )
            )
        self.assertEqual(items, [1, 2, 3, 4, 5])
        self.assertEqual(capped, [1, 2, 3])


class TokenRefreshTests(TestCase):
    def setUp(self):
        self.connection = Connection.objects.create(
            connector_key="example_oauth",
            label="Fictional calendar",
            enabled_capabilities=["calendar"],
            credentials={
                "access_token": "old",
                "refresh_token": "r1",
                "expires_at": time.time() + 30,
            },
        )
        ConnectorApp.objects.create(
            connector_key="example_oauth", client_id="id", client_secret="secret"
        )

    def test_fresh_tokens_are_used_as_they_are(self):
        self.connection.credentials = {"access_token": "a", "expires_at": time.time() + 3600}
        with mock.patch.object(oauth, "post_form") as post:
            self.assertEqual(oauth.access_token(self.connection), "a")
        post.assert_not_called()
        self.connection.credentials = {"access_token": "b"}
        self.assertEqual(oauth.access_token(self.connection), "b")

    def test_refresh_saves_rotated_tokens_once(self):
        tokens = {"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
        with mock.patch.object(oauth, "post_form", return_value=tokens) as post:
            connector = ExampleOAuth(self.connection)
            self.assertEqual(connector.access_token(), "new")
        sent = post.call_args.args[1]
        self.assertEqual(
            (sent["grant_type"], sent["refresh_token"], sent["client_id"]),
            ("refresh_token", "r1", "id"),
        )
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.credentials["refresh_token"], "r2")
        self.assertGreater(self.connection.credentials["expires_at"], time.time() + 3000)
        Connection.objects.filter(pk=self.connection.pk).update(
            credentials={**self.connection.credentials, "expires_at": time.time()}
        )
        self.connection.refresh_from_db()
        with mock.patch.object(oauth, "post_form", return_value={"access_token": "newer"}):
            oauth.access_token(self.connection)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.credentials["refresh_token"], "r2")

    def test_refused_or_missing_refresh_needs_sign_in(self):
        refused = oauth.TokenRefused("refused", status=400)
        with mock.patch.object(oauth, "post_form", side_effect=refused):
            with self.assertRaisesMessage(ConnectorError, "Sign in again"):
                oauth.access_token(self.connection)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, Connection.Status.NEEDS_REAUTH)
        Connection.objects.filter(pk=self.connection.pk).update(
            credentials={"access_token": "a", "expires_at": time.time()},
            status=Connection.Status.ACTIVE,
        )
        self.connection.refresh_from_db()
        with self.assertRaisesMessage(ConnectorError, "Sign in again"):
            oauth.access_token(self.connection)
        with mock.patch.object(oauth, "post_form", return_value={"token_type": "x"}):
            Connection.objects.filter(pk=self.connection.pk).update(
                credentials={"access_token": "a", "refresh_token": "r", "expires_at": time.time()}
            )
            self.connection.refresh_from_db()
            with self.assertRaisesMessage(ConnectorError, "did not return an access token"):
                oauth.access_token(self.connection)


class RecordTests(TestCase):
    def test_valid_records_have_no_problems(self):
        records = [
            CallRecord(
                external_id="c",
                occurred_at=NOW,
                direction="inbound",
                remote="+12025550143",
                duration_seconds=60,
                recording_url="https://r.example.test/1",
            ),
            TextRecord(
                external_id="t",
                occurred_at=NOW,
                direction="outbound",
                remote="+12025550143",
                body="Hi",
            ),
            EmailRecord(
                external_id="e", occurred_at=NOW, direction="inbound", remote="lena@example.test"
            ),
            CalendarEventRecord(external_id="v", occurred_at=NOW, title="Consult", ends_at=NOW),
            BookingRecord(
                external_id="b", occurred_at=NOW, title="Consult", invitee_email="lena@example.test"
            ),
            PaymentRecord(
                external_id="p", occurred_at=NOW, amount="250.00", deposit_account="trust"
            ),
            CaseStatusRecord(
                external_id="s",
                occurred_at=NOW,
                receipt_number="IOE0912345678",
                status_title="Case Was Received",
            ),
            CaseStatusRecord(
                external_id="s",
                occurred_at=NOW,
                receipt_number="IOE0912345678",
                problem="not_found",
                status_text="The source's own explanation.",
            ),
            LeadRecord(external_id="l", occurred_at=NOW, name="Lena", phone="+12025550144"),
            SignatureRecord(external_id="d", occurred_at=NOW, status="signed"),
            SignatureRecord(
                external_id="d", occurred_at=NOW, status="completed", signed_pdf=b"%PDF-1.7"
            ),
            ContactRecord(external_id="1", occurred_at=NOW, first_name="Lena"),
            ContactRecord(external_id="2", occurred_at=NOW, company="Fictional Employer LLC"),
            MatterRecord(
                external_id="m",
                occurred_at=NOW,
                client="1",
                title="Spousal petition",
                opened_on=NOW.date(),
                parties=(("2", "Employer"),),
            ),
        ]
        for record in records:
            self.assertEqual(record.problems(), [], record.kind)
        self.assertEqual(
            set(RECORD_TYPES),
            {
                "phone",
                "texting",
                "mailbox",
                "calendar",
                "scheduling",
                "payments",
                "case_status",
                "lead_capture",
                "esignature",
                "migration",
            },
        )
        self.assertEqual(
            ContactRecord(external_id="c", occurred_at=NOW, date_of_birth="1990").problems(),
            ["a name or company is required", "date_of_birth must be a date"],
        )
        broken = MatterRecord(
            external_id="m",
            occurred_at=NOW,
            client="",
            title="",
            status="archived",
            opened_on="2026",
            parties=(("", "Spouse"),),
        )
        self.assertEqual(len(broken.problems()), 5)
        self.assertEqual(len(LeadRecord(external_id="l", occurred_at=NOW).problems()), 2)
        unsigned = SignatureRecord(external_id="d", occurred_at=NOW, status="completed")
        self.assertEqual(unsigned.problems(), ["a completed signature needs the signed PDF"])
        self.assertEqual(
            len(SignatureRecord(external_id="d", occurred_at=NOW, status="x").problems()), 1
        )

    def test_problems(self):
        naive = datetime(2026, 3, 2, 15, 0)
        cases = [
            (
                CallRecord(
                    external_id="",
                    occurred_at=naive,
                    direction="sideways",
                    remote="",
                    duration_seconds=-1,
                    recording_url="http://x",
                ),
                5,
            ),
            (TextRecord(external_id="t", occurred_at=NOW, direction="x", remote=""), 1),
            (EmailRecord(external_id="e", occurred_at=NOW, direction="x", remote="nobody"), 2),
            (CalendarEventRecord(external_id="v", occurred_at=NOW, title="", ends_at=naive), 2),
            (BookingRecord(external_id="b", occurred_at=NOW, title=""), 2),
            (
                PaymentRecord(
                    external_id="p", occurred_at=NOW, amount="-1", status="x", deposit_account="x"
                ),
                3,
            ),
            (PaymentRecord(external_id="p", occurred_at=NOW, amount="abc"), 1),
            (
                CaseStatusRecord(
                    external_id="s", occurred_at=NOW, receipt_number="bad", status_title=""
                ),
                2,
            ),
            (
                CaseStatusRecord(
                    external_id="s",
                    occurred_at=NOW,
                    receipt_number="IOE0912345678",
                    status_title="Case Was Received",
                    problem="lost",
                ),
                2,
            ),
        ]
        for record, count in cases:
            self.assertEqual(len(record.problems()), count, record.problems())


class MatchingTests(TestCase):
    def test_phone_numbers(self):
        self.assertEqual(normalize_phone("(202) 555-0143"), "+12025550143")
        self.assertEqual(normalize_phone("+52 55 5555 0100"), "+525555550100")
        for value in ("", "555-0100", "not a number", "lena@example.test", "+1 555 555 0100"):
            self.assertEqual(normalize_phone(value), "")
        with override_settings(PHONE_DEFAULT_REGION="MX"):
            self.assertEqual(normalize_phone("55 5555 0100"), "+525555550100")
        self.assertEqual(counterpart_key(" Lena@Example.TEST "), "lena@example.test")
        self.assertEqual(counterpart_key("202.555.0143"), "+12025550143")

    def test_contacts_keep_e164_and_match_only_when_unique(self):
        lena = make_contact(first_name="Lena", email="Lena@Example.test", phone="(202) 555-0143")
        self.assertEqual(lena.phone_e164, "+12025550143")
        Contact.objects.filter(pk=lena.pk).update(phone="")
        lena.refresh_from_db()
        lena.phone = "+1 312 555 0188"
        lena.save(update_fields=["phone"])
        lena.refresh_from_db()
        self.assertEqual(lena.phone_e164, "+13125550188")
        self.assertEqual(match_contact("312-555-0188"), lena)
        self.assertEqual(match_contact("lena@example.test"), lena)
        self.assertIsNone(match_contact("312-555"))
        make_contact(first_name="Omar", email="omar@example.test", phone="312 555 0188")
        self.assertIsNone(match_contact("312-555-0188"))
        staff = make_user("paralegal", superuser=False)
        self.assertEqual(match_staff("PARALEGAL@example.test"), staff)
        self.assertIsNone(match_staff(""))

    def test_backfill_migrations(self):
        lena = make_contact(phone="(202) 555-0143")
        Contact.objects.filter(pk=lena.pk).update(phone_e164="")
        item = Communication.objects.create(
            channel="call", contact=lena, counterpart="202-555-0143"
        )
        Communication.objects.filter(pk=item.pk).update(counterpart_key="")
        importlib.import_module("crm.migrations.0007_contact_phone_e164").fill_phone_e164(
            apps, None
        )
        importlib.import_module(
            "crm.comms.migrations.0003_communication_counterpart_key"
        ).fill_counterpart_key(apps, None)
        lena.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((lena.phone_e164, item.counterpart_key), ("+12025550143", "+12025550143"))
