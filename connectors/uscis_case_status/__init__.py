"""USCIS Case Status API, through the firm's own developer.uscis.gov app.

The firm registers an app at developer.uscis.gov and pastes its client ID and
secret; the connector gets access tokens with the client credentials grant.

In the sandbox, it checks USCIS's published test receipt numbers once a day.
That builds the days of test traffic USCIS asks for before it grants production
access, and client receipt numbers never go to the sandbox. In production, it
checks the receipt numbers core lists as due (those on open matters, about once a
day each), spacing requests well under USCIS's limits and stopping at a rate
limit or a run of failures. What it finds is recorded on the matter in core.

API reference: https://developer.uscis.gov/api/case-status
"""

import re
import time
from datetime import timedelta
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.html import strip_tags

from crm.connect import lookups
from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
    SyncBatch,
)
from crm.connect.http import ProviderClient
from crm.connect.records import INVALID_RECEIPT, NOT_FOUND, UNAVAILABLE, CaseStatusRecord
from crm.connect.registry import register

SANDBOX, PRODUCTION = "sandbox", "production"
SANDBOX_URL = "https://api-int.uscis.gov"
PRODUCTION_URL = "https://api.uscis.gov"
# Test receipt numbers USCIS publishes; the sandbox answers only for these.
STAGING_RECEIPTS = ("EAC9999103403", "EAC9999103400", "LIN9999106498", "SRC9999102777")
# A test-range number USCIS does not publish, so its not-found answer is exercised too.
STAGING_NOT_FOUND = "EAC9999199999"
# USCIS allows 5 requests a second in the sandbox and 10 in production.
REQUEST_SPACING = 0.5
BATCH_SIZE = 300
# A run stops after this many receipt numbers in a row get no answer.
MAX_FAILURES = 3
# Answers that mean the whole run should stop: bad token, no access, rate limit.
STOP_STATUSES = {401, 403, 429}
RECEIPT_LIKE = re.compile(r"\b[A-Za-z]{3}\*?\d{9,10}\b")


def validate_production_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not (host == "uscis.gov" or host.endswith(".uscis.gov"))
        or parts.path not in ("", "/")
        or parts.query
    ):
        raise ValidationError(
            "Enter the https:// address of a uscis.gov API, such as https://api.uscis.gov."
        )


def clean(value):
    """USCIS text without markup or extra spaces."""
    return " ".join(strip_tags(str(value or "")).split())


def scrub(message):
    """Remove anything shaped like a receipt number from a message stored on the connection."""
    return RECEIPT_LIKE.sub("[receipt number]", message)


def uscis_message(response):
    """The ``message`` USCIS puts in an answer, in either of its error formats."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        body = errors[0]
    return clean(body.get("message"))[:300]


@register
class USCISCaseStatusConnector(Connector):
    key = "uscis_case_status"
    name = "USCIS Case Status API"
    description = (
        "Checks the receipt numbers of open matters once a day through the firm's own "
        "developer.uscis.gov app. A changed status creates a review task for staff."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.CASE_STATUS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "client_id",
                "Client ID",
                secret=False,
                help_text="From the firm's app at developer.uscis.gov.",
            ),
            CredentialField("client_secret", "Client secret"),
        ),
        docs_url="https://developer.uscis.gov/get-started/sign-up",
        label="Client ID and secret",
    )
    settings_fields = (
        SettingField(
            "environment",
            "Environment",
            choices=(
                (SANDBOX, "Sandbox: USCIS test receipt numbers only"),
                (PRODUCTION, "Production: the receipt numbers of open matters"),
            ),
            default=SANDBOX,
            help_text="Start in the sandbox. Once USCIS grants production access, connect "
            "again with the production client ID and secret.",
        ),
        SettingField(
            "production_url",
            "Production API address",
            default=PRODUCTION_URL,
            help_text="Used only in production. USCIS confirms it when it grants access.",
            validate=validate_production_url,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The firm's USCIS client ID and secret, to get an access token",
            "In production: the receipt numbers of open matters, about once a day each",
            "In the sandbox: only USCIS's published test receipt numbers",
        ),
        receives=("The current case status title and description for each receipt number",),
    )
    sync_capabilities = frozenset({Capability.CASE_STATUS})
    # Each run checks only what is due, so most hourly runs send nothing.
    sync_interval = timedelta(hours=1)

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            self.base_url,
            name="USCIS",
            headers={"Accept": "application/json"},
            min_interval=REQUEST_SPACING,
        )
        self.token = ""
        self.token_expires = 0.0

    @property
    def sandbox(self):
        return self.setting("environment") != PRODUCTION

    @property
    def base_url(self):
        return SANDBOX_URL if self.sandbox else self.setting("production_url").rstrip("/")

    def access_token(self, *, renew=False):
        """A bearer token from the client credentials grant, reused until it nearly expires."""
        if self.token and not renew and time.monotonic() < self.token_expires:
            return self.token
        try:
            answer = self.client.post(
                "/oauth/accesstoken",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.get("client_id", ""),
                    "client_secret": self.credentials.get("client_secret", ""),
                },
            )
        except ConnectorError as exc:
            if exc.status in (400, 401):
                raise ConnectorError(
                    "USCIS did not accept the client ID and secret.", status=exc.status
                ) from None
            raise
        token = str(answer.get("access_token", "")) if isinstance(answer, dict) else ""
        if not token:
            raise ConnectorError("USCIS did not return an access token.")
        try:
            lifetime = int(answer.get("expires_in") or 1800)
        except (TypeError, ValueError):
            lifetime = 1800
        self.token, self.token_expires = token, time.monotonic() + max(lifetime - 60, 60)
        return token

    def fetch(self, receipt_number):
        """USCIS's answer for one receipt number, getting a new token once if it expired."""
        for renew in (False, True):
            response = self.client.request(
                "GET",
                f"/case-status/{receipt_number}",
                expected=(200, 401, 403, 404, 422),
                headers={"Authorization": f"Bearer {self.access_token(renew=renew)}"},
            )
            if response.status_code != 401:
                break
        if response.status_code == 401:
            raise ConnectorError(
                scrub(f"USCIS rejected the access token: {uscis_message(response)}"), status=401
            )
        if response.status_code == 403:
            raise ConnectorError(
                scrub(
                    "USCIS refused access. Check that the firm's app includes the Case Status "
                    f"API: {uscis_message(response)}"
                ),
                status=403,
            )
        return response

    def lookup(self, receipt_number):
        """One receipt number's case status, or the problem USCIS reported for it."""
        response = self.fetch(receipt_number)
        checked_at = timezone.now()
        common = {
            "external_id": f"{receipt_number}@{checked_at:%Y%m%dT%H%M%S}",
            "occurred_at": checked_at,
            "receipt_number": receipt_number,
        }
        if response.status_code == 404:
            return CaseStatusRecord(
                **common, problem=NOT_FOUND, status_text=uscis_message(response)
            )
        if response.status_code == 422:
            return CaseStatusRecord(
                **common, problem=INVALID_RECEIPT, status_text=uscis_message(response)
            )
        try:
            status = response.json().get("case_status") or {}
            title = clean(status.get("current_case_status_text_en"))[:255]
            text = clean(status.get("current_case_status_desc_en"))
        except (AttributeError, ValueError):
            title = ""
        if not title:
            raise ConnectorError("USCIS sent a case status the CRM did not understand.")
        return CaseStatusRecord(**common, status_title=title, status_text=text)

    def explain(self, exc):
        if exc.status == 429:
            return "USCIS's request limit was reached."
        if exc.status == 503 and self.sandbox:
            return f"{exc} The USCIS sandbox is open weekdays from 7 AM to 8 PM Eastern."
        return scrub(str(exc))

    def test_connection(self):
        self.access_token()
        if self.sandbox:
            try:
                record = self.lookup(STAGING_RECEIPTS[0])
            except ConnectorError as exc:
                raise ConnectorError(self.explain(exc), status=exc.status) from None
            if record.problem:
                raise ConnectorError("The USCIS sandbox did not answer for its own test receipt.")

    def sync(self, capability, cursor):
        cursor = dict(cursor or {})
        if self.sandbox:
            today = timezone.localdate().isoformat()
            if cursor.get("sandbox_checked_on") == today:
                return SyncBatch(records=[], cursor=cursor)
            receipts = [*STAGING_RECEIPTS, STAGING_NOT_FOUND]
            cursor["sandbox_checked_on"] = today
        else:
            receipts = lookups.due(Capability.CASE_STATUS, limit=BATCH_SIZE)
        if not receipts:
            return SyncBatch(records=[], cursor=cursor)
        try:
            self.access_token()
        except ConnectorError as exc:
            raise ConnectorError(self.explain(exc), status=exc.status) from None
        records, failures, warning = [], 0, ""
        for receipt in receipts:
            try:
                records.append(self.lookup(receipt))
                failures = 0
                continue
            except ConnectorError as exc:
                failure = exc
            failures += 1
            if failure.status not in STOP_STATUSES and failures < MAX_FAILURES:
                checked_at = timezone.now()
                records.append(
                    CaseStatusRecord(
                        external_id=f"{receipt}@{checked_at:%Y%m%dT%H%M%S}",
                        occurred_at=checked_at,
                        receipt_number=receipt,
                        problem=UNAVAILABLE,
                    )
                )
                continue
            if not records:
                raise ConnectorError(self.explain(failure), status=failure.status) from None
            warning = (
                f"{self.explain(failure)} Stopped after {len(records)} of {len(receipts)} "
                "receipt numbers; the rest are checked in a later run."
            )
            break
        return SyncBatch(records=records, cursor=cursor, warning=warning)
