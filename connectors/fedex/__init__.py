"""FedEx: tracking for the packages a firm ships, through FedEx's Basic Integrated
Visibility API (formerly the Track API).

The firm creates a project in the FedEx Developer Portal, linked to its FedEx
account, and enters the project's API key and secret key. Each hour, the worker asks
core for the FedEx tracking numbers due for a check and looks them up, 30 to a
request, with a token from the client credentials grant that is reused until it
nearly expires, as FedEx asks. It reports each package's status in the CRM's terms,
FedEx's description of the latest scan, the delivery time, and the expected
delivery date.

FedEx licenses tracking for shipments the firm itself sent. Packages sent with
FedEx Authenticated Delivery are not shared through this API.

API reference: https://developer.fedex.com/api/en-us/catalog/track/v1/docs.html
"""

import time
from datetime import date, datetime, timedelta

from django.utils import timezone

from crm.connect import lookups
from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SyncBatch,
)
from crm.connect.http import ProviderClient
from crm.connect.records import NOT_FOUND, NOT_SHARED, UNAVAILABLE, ShipmentRecord
from crm.connect.registry import register

API_URL = "https://apis.fedex.com"
CARRIER = "fedex"
# FedEx takes at most 30 tracking numbers a request.
BATCH_SIZE = 30
PER_RUN = 120
# FedEx's status codes in the CRM's terms. FedEx warns its codes can grow, so any
# code not listed is taken as in transit.
STATUSES = {
    "OC": "sent",
    "PD": "sent",
    "OD": "out_for_delivery",
    "DL": "delivered",
    "DE": "problem",
    "SE": "problem",
    "CA": "problem",
    "HP": "problem",
    "RS": "returned",
}


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def parse_day(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def when(result, kind):
    for item in result.get("dateAndTimes") or []:
        if item.get("type") == kind:
            return item.get("dateTime")
    return None


def status_of(result):
    latest = result.get("latestStatusDetail") or {}
    code = str(latest.get("derivedCode") or latest.get("code") or "").upper()
    words = str(latest.get("description") or latest.get("statusByLocale") or "").lower()
    if "return" in words and "shipper" in words:
        return "returned"
    return STATUSES.get(code, "in_transit")


def detail_of(result):
    """FedEx's description of the latest scan, where it was, and any reasons it gives."""
    latest = result.get("latestStatusDetail") or {}
    place = latest.get("scanLocation") or {}
    words = latest.get("description") or latest.get("statusByLocale")
    scan = ", ".join(
        str(p) for p in (words, place.get("city"), place.get("stateOrProvinceCode")) if p
    )
    reasons = [
        str(item["reasonDescription"])
        for item in latest.get("ancillaryDetails") or []
        if item.get("reasonDescription")
    ]
    return ". ".join(part for part in (scan, *reasons) if part)


def problem_of(error):
    code = str((error or {}).get("code") or "").upper()
    if "AUTHENTICATEDDELIVERY" in code:
        return NOT_SHARED
    if "NOTFOUND" in code or "INVALID" in code:
        return NOT_FOUND
    return UNAVAILABLE


@register
class FedexConnector(Connector):
    key = "fedex"
    name = "FedEx"
    description = (
        "Track filing packages sent by FedEx and note their delivery, or any problem, on the "
        "matter."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SHIPPING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField("client_id", "API key", secret=False),
            CredentialField("client_secret", "Secret key"),
        ),
        docs_url="https://developer.fedex.com/api/en-us/get-started/shipper.html",
        label="Project keys",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The firm's FedEx API key and secret key, to get an access token",
            "The tracking numbers of the firm's FedEx shipments still on their way, about "
            "every four hours each",
        ),
        receives=(
            "Each package's status, FedEx's description of its latest scan, when it was "
            "delivered, and when it is expected",
        ),
    )
    sync_capabilities = frozenset({Capability.SHIPPING})
    # Each run checks only the shipments due, so most hourly runs send little or nothing.
    sync_interval = timedelta(hours=1)

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL, name="FedEx", headers={"Accept": "application/json", "X-locale": "en_US"}
        )
        self.token = ""
        self.token_expires = 0.0

    def access_token(self, *, renew=False):
        """A bearer token from the client credentials grant, reused until it nearly expires."""
        if self.token and not renew and time.monotonic() < self.token_expires:
            return self.token
        try:
            answer = self.client.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.get("client_id", ""),
                    "client_secret": self.credentials.get("client_secret", ""),
                },
            )
        except ConnectorError as exc:
            if exc.status in (400, 401):
                raise ConnectorError(
                    "FedEx did not accept the API key and secret key.", status=exc.status
                ) from None
            raise
        token = str(answer.get("access_token", "")) if isinstance(answer, dict) else ""
        if not token:
            raise ConnectorError("FedEx did not return an access token.")
        try:
            lifetime = int(answer.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        self.token, self.token_expires = token, time.monotonic() + max(lifetime - 60, 60)
        return token

    def track(self, numbers):
        """FedEx's results for up to 30 tracking numbers, by tracking number."""
        body = {
            "includeDetailedScans": False,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": n}} for n in numbers],
        }
        for renew in (False, True):
            response = self.client.request(
                "POST",
                "/track/v1/trackingnumbers",
                json=body,
                expected=(200, 401, 403),
                headers={"Authorization": f"Bearer {self.access_token(renew=renew)}"},
            )
            if response.status_code != 401:
                break
        if response.status_code in (401, 403):
            raise ConnectorError(
                "FedEx refused tracking. Check that the firm's project includes Basic "
                "Integrated Visibility (tracking) and its production key.",
                status=response.status_code,
            )
        try:
            answer = response.json()
        except ValueError:
            raise ConnectorError("FedEx sent a response that was not understood.") from None
        found = {}
        for item in (answer.get("output") or {}).get("completeTrackResults") or []:
            results = item.get("trackResults") or []
            if results:
                found[str(item.get("trackingNumber") or "").upper()] = results[0]
        return found

    def record(self, number, result, checked_at):
        common = {
            "external_id": f"{number}@{checked_at:%Y%m%dT%H%M%S}",
            "occurred_at": checked_at,
            "carrier": CARRIER,
            "tracking_number": number,
        }
        if result is None or result.get("error"):
            return ShipmentRecord(**common, problem=problem_of((result or {}).get("error")))
        status = status_of(result)
        window = (result.get("estimatedDeliveryTimeWindow") or {}).get("window") or {}
        return ShipmentRecord(
            **common,
            status=status,
            detail=detail_of(result)[:255],
            delivered_at=parse_time(when(result, "ACTUAL_DELIVERY"))
            if status == "delivered"
            else None,
            expected_on=parse_day(when(result, "ESTIMATED_DELIVERY") or window.get("ends")),
        )

    def test_connection(self):
        self.access_token()

    def sync(self, capability, cursor):
        if capability != Capability.SHIPPING:
            raise ConnectorError(f"FedEx does not sync {capability}.")
        numbers = lookups.due(Capability.SHIPPING, limit=PER_RUN, carrier=CARRIER)
        records = []
        for start in range(0, len(numbers), BATCH_SIZE):
            batch = numbers[start : start + BATCH_SIZE]
            found = self.track(batch)
            checked_at = timezone.now()
            records += [self.record(n, found.get(n), checked_at) for n in batch]
        return SyncBatch(records=records, cursor={})
