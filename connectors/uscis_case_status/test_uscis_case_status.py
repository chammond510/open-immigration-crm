from datetime import date, timedelta
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect import http, lookups
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, SyncRun
from crm.connect.records import NOT_FOUND, UNAVAILABLE
from crm.connect.services import create_connection
from crm.connect.tasks import run_sync
from crm.connect.testing import ConnectorContract
from crm.models import WorkItem
from crm.notices.models import CaseNotice, CaseStatusCheck, CaseStatusProblem
from crm.tests.factories import make_contact, make_matter, make_user

from . import (
    PRODUCTION,
    REQUEST_SPACING,
    STAGING_NOT_FOUND,
    STAGING_RECEIPTS,
    USCISCaseStatusConnector,
    validate_production_url,
)

CREDENTIALS = {"client_id": "fictional-client", "client_secret": "fictional-secret"}
NOT_FOUND_MESSAGE = "Case Status Online does not recognize the receipt number entered."


class FakeUSCIS:
    """Answers like the USCIS sandbox: tokens, then case status by receipt number.

    ``answers`` maps a receipt number to a status title, or to an HTTP status code
    for an error answer. Unlisted staging receipts are approved; others are not found.
    """

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.requests = []
        self.tokens = 0
        self.rejected_tokens = set()

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/oauth/accesstoken":
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != [CREDENTIALS["client_secret"]]:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens += 1
            # USCIS's gateway names the type "BearerToken"; requests still send "Bearer".
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.tokens}",
                    "expires_in": "1799",
                    "token_type": "BearerToken",
                },
            )
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.rejected_tokens:
            return httpx.Response(401, json={"code": 401, "message": "Invalid Access Token"})
        receipt = request.url.path.rsplit("/", 1)[-1]
        default = "Case Was Approved" if receipt in STAGING_RECEIPTS else 404
        answer = self.answers.get(receipt, default)
        if isinstance(answer, int):
            message = NOT_FOUND_MESSAGE if answer == 404 else f"Fictional error {answer}"
            return httpx.Response(answer, json={"code": answer, "message": message})
        return httpx.Response(
            200,
            json={
                "case_status": {
                    "receiptNumber": receipt,
                    "formType": "I-130",
                    "current_case_status_text_en": answer,
                    "current_case_status_desc_en": f"<p>On October 30, 2026, we updated  "
                    f"your Form I-130, Receipt Number {receipt}.</p>",
                },
                "message": "Query was successful",
            },
        )

    def receipts_requested(self):
        return [
            request.url.path.rsplit("/", 1)[-1]
            for request in self.requests
            if "/case-status/" in request.url.path
        ]


class FakeUSCISMixin:
    def setUp(self):
        super().setUp()
        self.uscis = FakeUSCIS()
        self.waits = []
        transport = http.use_transport(httpx.MockTransport(lambda r: self.uscis(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)


class USCISContractTests(FakeUSCISMixin, ConnectorContract, TestCase):
    connector_class = USCISCaseStatusConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "client_secret": "wrong"}
    sync_examples = {"case_status": {}}


class USCISConnectorTests(FakeUSCISMixin, TestCase):
    def connect(self, environment="sandbox", **config):
        return Connection.objects.create(
            connector_key="uscis_case_status",
            label="USCIS",
            enabled_capabilities=["case_status"],
            credentials=dict(CREDENTIALS),
            config={"environment": environment, **config},
        )

    def connector(self, environment="sandbox", **config):
        return USCISCaseStatusConnector(self.connect(environment, **config))

    def test_sandbox_checks_only_test_receipts_once_a_day(self):
        connector = self.connector()
        with mock.patch.object(lookups, "due", side_effect=AssertionError("asked core")):
            batch = connector.sync("case_status", {})
            again = connector.sync("case_status", batch.cursor)
        self.assertEqual(self.uscis.receipts_requested(), [*STAGING_RECEIPTS, STAGING_NOT_FOUND])
        self.assertTrue(all(r.url.host == "api-int.uscis.gov" for r in self.uscis.requests))
        self.assertEqual([r.status_title for r in batch.records[:4]], ["Case Was Approved"] * 4)
        self.assertEqual(batch.records[4].problem, NOT_FOUND)
        self.assertEqual(batch.records[4].status_text, NOT_FOUND_MESSAGE)
        self.assertEqual(batch.cursor, {"sandbox_checked_on": timezone.localdate().isoformat()})
        self.assertEqual(again.records, [])
        self.assertEqual(len(self.uscis.requests), 6)

    def test_token_is_requested_once_and_sent_as_bearer(self):
        self.connector().sync("case_status", {})
        token_request = self.uscis.requests[0]
        self.assertEqual(token_request.method, "POST")
        self.assertEqual(
            parse_qs(token_request.content.decode()),
            {
                "grant_type": ["client_credentials"],
                "client_id": ["fictional-client"],
                "client_secret": ["fictional-secret"],
            },
        )
        self.assertEqual(self.uscis.tokens, 1)
        self.assertEqual(
            {r.headers["Authorization"] for r in self.uscis.requests[1:]}, {"Bearer token-1"}
        )

    def test_requests_are_spaced_under_the_limit(self):
        self.connector().sync("case_status", {})
        self.assertEqual(len(self.waits), len(self.uscis.requests) - 1)
        self.assertTrue(all(0 < wait <= REQUEST_SPACING for wait in self.waits))
        self.assertGreater(min(self.waits), REQUEST_SPACING - 0.1)

    def test_an_expired_token_is_renewed_once(self):
        self.uscis.rejected_tokens = {"token-1"}
        batch = self.connector().sync("case_status", {})
        self.assertEqual(self.uscis.tokens, 2)
        self.assertEqual(len(batch.records), 5)
        self.uscis.rejected_tokens = {f"token-{number}" for number in range(1, 10)}
        with self.assertRaisesMessage(ConnectorError, "USCIS rejected the access token"):
            self.connector().sync("case_status", {})

    def test_html_is_stripped_from_status_text(self):
        self.uscis.answers = {STAGING_RECEIPTS[0]: "<b>Case Was Received</b>"}
        record = self.connector().lookup(STAGING_RECEIPTS[0])
        self.assertEqual(record.status_title, "Case Was Received")
        self.assertEqual(
            record.status_text,
            f"On October 30, 2026, we updated your Form I-130, Receipt Number "
            f"{STAGING_RECEIPTS[0]}.",
        )
        self.uscis.answers = {STAGING_RECEIPTS[0]: ""}
        with self.assertRaisesMessage(ConnectorError, "did not understand"):
            self.connector().lookup(STAGING_RECEIPTS[0])

    def test_connection_test(self):
        self.connector().test_connection()
        self.assertEqual(self.uscis.receipts_requested(), [STAGING_RECEIPTS[0]])
        self.uscis.requests.clear()
        self.connector(PRODUCTION).test_connection()
        self.assertEqual(len(self.uscis.requests), 1)
        self.assertEqual(str(self.uscis.requests[0].url), "https://api.uscis.gov/oauth/accesstoken")
        bad = self.connect()
        bad.credentials = {**CREDENTIALS, "client_secret": "wrong"}
        with self.assertRaisesMessage(ConnectorError, "did not accept the client ID and secret"):
            USCISCaseStatusConnector(bad).test_connection()
        self.uscis.answers = {STAGING_RECEIPTS[0]: 503}
        with self.assertRaisesMessage(ConnectorError, "open weekdays from 7 AM to 8 PM Eastern"):
            self.connector().test_connection()
        self.uscis.answers = {STAGING_RECEIPTS[0]: 404}
        with self.assertRaisesMessage(ConnectorError, "did not answer for its own test receipt"):
            self.connector().test_connection()

    def test_refused_access_is_explained_without_receipt_numbers(self):
        def refuse(request):
            if request.url.path == "/oauth/accesstoken":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 1799})
            receipt = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(403, json={"errors": [{"message": f"No access for {receipt}"}]})

        self.uscis = refuse
        with self.assertRaises(ConnectorError) as raised:
            self.connector().sync("case_status", {})
        message = str(raised.exception)
        self.assertIn("Check that the firm's app includes the Case Status API", message)
        self.assertNotIn(STAGING_RECEIPTS[0], message)
        self.assertIn("No access for [receipt number]", message)

    def test_production_address_must_be_a_uscis_api(self):
        validate_production_url("https://api.uscis.gov")
        validate_production_url("https://api.uscis.gov/")
        for address in (
            "http://api.uscis.gov",
            "https://api.uscis.gov.example.test",
            "https://uscis.gov.example.test",
            "https://api.uscis.gov/case-status",
            "https://api.uscis.gov/?x=1",
        ):
            with self.assertRaises(ValidationError, msg=address):
                validate_production_url(address)
        admin = make_user()
        with self.assertRaises(ValidationError):
            create_connection(
                "uscis_case_status",
                label="USCIS",
                created_by=admin,
                capabilities=["case_status"],
                credentials=dict(CREDENTIALS),
                config={"environment": PRODUCTION, "production_url": "https://example.test"},
            )
        connection = create_connection(
            "uscis_case_status",
            label="USCIS",
            created_by=admin,
            capabilities=["case_status"],
            credentials=dict(CREDENTIALS),
        )
        self.assertEqual(
            connection.config,
            {"environment": "sandbox", "production_url": "https://api.uscis.gov"},
        )


class ConnectPageTests(FakeUSCISMixin, TestCase):
    def test_settings_are_chosen_when_connecting_and_shown_after(self):
        self.client.force_login(make_user())
        url = reverse("connector_detail", args=["uscis_case_status"])
        page = self.client.get(url)
        self.assertContains(page, "Sandbox: USCIS test receipt numbers only")
        self.assertContains(page, 'value="https://api.uscis.gov"')
        form = {
            "label": "USCIS",
            "capabilities": ["case_status"],
            "credential_client_id": "fictional-client",
            "credential_client_secret": "fictional-secret",
            "setting_environment": "sandbox",
            "setting_production_url": "https://api.example.test",
            "acknowledge": "on",
        }
        refused = self.client.post(url, form)
        self.assertContains(refused, "Enter the https:// address of a uscis.gov API")
        self.assertFalse(Connection.objects.exists())
        response = self.client.post(
            url, {**form, "setting_production_url": "https://api.uscis.gov"}
        )
        connection = Connection.objects.get()
        self.assertRedirects(response, reverse("connection_detail", args=[connection.pk]))
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        self.assertEqual(connection.config["environment"], "sandbox")
        detail = self.client.get(reverse("connection_detail", args=[connection.pk]))
        self.assertContains(
            detail, "<dt>Environment</dt><dd>Sandbox: USCIS test receipt", html=False
        )
        self.assertNotContains(detail, "fictional-secret")


class ProductionChecksTests(FakeUSCISMixin, TestCase):
    """The production path end to end: core lists receipts, the worker records the answers."""

    def setUp(self):
        super().setUp()
        self.owner = make_user("paralegal", superuser=False)
        self.connection = Connection.objects.create(
            connector_key="uscis_case_status",
            label="USCIS",
            enabled_capabilities=["case_status"],
            credentials=dict(CREDENTIALS),
            config={"environment": PRODUCTION, "production_url": "https://api.uscis.gov"},
        )
        self.matters = [
            make_matter(
                make_contact(first_name=name, email=f"{name.lower()}@example.test"),
                self.owner,
                receipt_number=receipt,
            )
            for name, receipt in (
                ("Amina", "IOE0912345601"),
                ("Ben", "IOE0912345602"),
                ("Chen", "IOE0912345603"),
            )
        ]

    def sync(self):
        run = SyncRun.objects.create(connection=self.connection, capability="case_status")
        run_sync.call(run.pk)
        run.refresh_from_db()
        self.connection.refresh_from_db()
        return run

    def test_daily_check_records_statuses_and_asks_for_review_on_change(self):
        self.uscis.answers = {
            "IOE0912345601": "Case Was Received",
            "IOE0912345602": "Case Is Being Actively Reviewed By USCIS",
            "IOE0912345603": "Case Was Received",
        }
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED)
        self.assertEqual(run.records_received, 3)
        self.assertTrue(all(r.url.host == "api.uscis.gov" for r in self.uscis.requests))
        self.assertEqual(CaseStatusCheck.objects.count(), 3)
        self.assertFalse(WorkItem.objects.exists())

        # Nothing is due again until about a day later.
        self.uscis.requests.clear()
        self.assertEqual(self.sync().records_received, 0)
        self.assertEqual(self.uscis.requests, [])

        CaseStatusCheck.objects.update(checked_at=timezone.now() - timedelta(hours=21))
        self.uscis.answers["IOE0912345601"] = "Request for Additional Evidence Was Sent"
        self.sync()
        task = WorkItem.objects.get()
        self.assertEqual(task.matter, self.matters[0])
        self.assertEqual(task.priority, WorkItem.Priority.URGENT)
        self.assertEqual(task.assigned_to, self.owner)
        self.assertIn("Review USCIS request for evidence", task.title)
        self.assertIn(
            'from "Case Was Received" to "Request for Additional Evidence', task.description
        )
        self.assertEqual(CaseStatusCheck.objects.filter(changed=True).get().review_task, task)

    def test_rate_limit_keeps_what_was_checked_and_stops(self):
        self.uscis.answers = {"IOE0912345601": "Case Was Received", "IOE0912345602": 429}
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED)
        self.assertIn("request limit was reached", run.error)
        self.assertIn("Stopped after 1 of 3", run.error)
        self.assertEqual(self.connection.last_error, run.error)
        self.assertEqual(self.connection.status, Connection.Status.ACTIVE)
        self.assertEqual(CaseStatusCheck.objects.get().receipt_number, "IOE0912345601")
        self.assertNotIn("IOE0912345603", self.uscis.receipts_requested())
        # The client retried with backoff before giving up.
        self.assertEqual(self.uscis.receipts_requested().count("IOE0912345602"), 4)

    def test_rate_limit_on_the_first_receipt_fails_the_run(self):
        self.uscis.answers = {"IOE0912345601": 429}
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.FAILED)
        self.assertEqual(run.error, "USCIS's request limit was reached.")
        self.assertNotIn("IOE", self.connection.last_error)

    def test_unanswered_receipts_are_marked_and_a_run_of_them_stops(self):
        extra = make_matter(
            make_contact(first_name="Dara", email="dara@example.test"),
            receipt_number="IOE0912345604",
        )
        self.uscis.answers = {
            receipt: 500
            for receipt in ("IOE0912345601", "IOE0912345602", "IOE0912345603", "IOE0912345604")
        }
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED)
        self.assertIn("Stopped after 2 of 4", run.error)
        problems = CaseStatusProblem.objects.all()
        self.assertEqual({p.kind for p in problems}, {UNAVAILABLE})
        self.assertEqual(problems.count(), 2)
        self.assertNotIn(extra.receipt_number, self.uscis.receipts_requested())

    def test_unknown_receipts_are_kept_as_problems_until_answered(self):
        CaseNotice.objects.create(
            matter=self.matters[0],
            kind="receipt",
            receipt_number="IOE0912345609",
            notice_date=date(2026, 1, 5),
        )
        self.uscis.answers = {
            "IOE0912345601": 404,
            "IOE0912345602": 422,
            "IOE0912345603": "Case Was Received",
            "IOE0912345609": "Case Was Received",
        }
        self.sync()
        problems = {p.receipt_number: p for p in CaseStatusProblem.objects.all()}
        self.assertEqual(problems["IOE0912345601"].kind, NOT_FOUND)
        self.assertEqual(problems["IOE0912345601"].detail, NOT_FOUND_MESSAGE)
        self.assertEqual(problems["IOE0912345602"].kind, "invalid_receipt")
        self.assertEqual(CaseStatusCheck.objects.count(), 2)

        CaseStatusProblem.objects.update(last_seen_at=timezone.now() - timedelta(hours=21))
        CaseStatusCheck.objects.update(checked_at=timezone.now() - timedelta(hours=21))
        self.uscis.answers["IOE0912345601"] = "Case Was Received"
        self.sync()
        self.assertEqual(
            set(CaseStatusProblem.objects.values_list("receipt_number", flat=True)),
            {"IOE0912345602"},
        )
