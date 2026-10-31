"""USPS: tracking for the packages a firm mails, through the USPS Tracking API (v3r2).

The firm registers an app in the USPS Business Portal and enters its consumer key
and secret. Each hour, the worker asks core for the USPS tracking numbers due for a
check and looks them up, 35 to a request, signing in with the client credentials
grant. It reports each package's status in the CRM's terms, USPS's own one-line
summary, the delivery time, and the expected delivery date.

Since April 2026, USPS shares tracking through its API only for packages labelled
under a Mailer ID the firm's account owns. A package labelled at a Post Office
counter, or under another company's account, comes back as not shared, and core
stops checking it.

API reference: https://developers.usps.com/trackingv3r2
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

API_URL = "https://apis.usps.com"
CARRIER = "usps"
# USPS takes at most 35 tracking numbers a request; one run sends a few requests.
BATCH_SIZE = 35
PER_RUN = 105
# USPS's status categories, old and new names, in the CRM's terms.
CATEGORIES = {
    "usps awaiting item": "sent",
    "pre-shipment": "sent",
    "on its way to usps": "sent",
    "accepted": "in_transit",
    "on the way": "in_transit",
    "in transit": "in_transit",
    "in transit from origin processing": "in_transit",
    "moving through network": "in_transit",
    "processing at destination": "in_transit",
    "preparing for delivery": "in_transit",
    "international transit": "in_transit",
    "out for delivery": "out_for_delivery",
    "delivered": "delivered",
    "delivered to agent": "delivered",
    "available for pickup": "problem",
    "available for agent pickup": "problem",
    "delivery attempt": "problem",
    "delivery attempt: action needed": "problem",
    "alert": "problem",
}
DELIVERY_CODES = {"01", "17", "43"}
# Event codes for a package on its way back to the sender, or refused.
RETURN_CODES = {"04", "09", "21", "22", "23", "24", "25", "26", "27", "28", "29", "UZ"}


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


def status_of(item):
    """The CRM's status for one package, from USPS's category and its latest event."""
    events = item.get("trackingEvents") or []
    latest = events[0] if events else {}
    code = str(latest.get("eventCode") or "").upper()
    words = f"{item.get('status', '')} {latest.get('eventType', '')}".lower()
    status = CATEGORIES.get(str(item.get("statusCategory") or "").strip().lower(), "in_transit")
    if code in RETURN_CODES or "return to sender" in words or "returned to sender" in words:
        return "returned"
    if status == "delivered" and "original sender" in words:
        return "returned"
    return status


def delivered_at(item):
    for event in item.get("trackingEvents") or []:
        if str(event.get("eventCode") or "") in DELIVERY_CODES:
            return parse_time(event.get("GMTTimestamp")) or parse_time(event.get("eventTimestamp"))
    return None


def problem_of(item):
    """Why USPS gave no status for one tracking number, from its own error."""
    code = str(item.get("statusCode") or "")
    error = item.get("error") or {}
    words = f"{error.get('code', '')} {error.get('message', '')}".lower()
    if code in ("401", "403") or any(w in words for w in ("mailer", "authoriz", "access")):
        return NOT_SHARED
    if code == "404" or "not found" in words:
        return NOT_FOUND
    return UNAVAILABLE


@register
class UspsConnector(Connector):
    key = "usps"
    name = "USPS"
    description = (
        "Track filing packages sent by USPS and note their delivery, or any problem, on the "
        "matter. USPS shares tracking only for packages labelled under the firm's own Mailer ID."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SHIPPING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField("client_id", "Consumer key", secret=False),
            CredentialField("client_secret", "Consumer secret"),
        ),
        docs_url="https://developers.usps.com/getting-started",
        label="App credentials",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The firm's USPS consumer key and secret, to get an access token",
            "The tracking numbers of the firm's USPS shipments still on their way, about every "
            "four hours each",
        ),
        receives=(
            "Each package's status, USPS's summary of its latest event, when it was delivered, "
            "and when it is expected",
        ),
    )
    sync_capabilities = frozenset({Capability.SHIPPING})
    # Each run checks only the shipments due, so most hourly runs send little or nothing.
    sync_interval = timedelta(hours=1)

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(API_URL, name="USPS", headers={"Accept": "application/json"})
        self.token = ""
        self.token_expires = 0.0

    def access_token(self, *, renew=False):
        """A bearer token from the client credentials grant, reused until it nearly expires."""
        if self.token and not renew and time.monotonic() < self.token_expires:
            return self.token
        try:
            answer = self.client.post(
                "/oauth2/v3/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.get("client_id", ""),
                    "client_secret": self.credentials.get("client_secret", ""),
                },
            )
        except ConnectorError as exc:
            if exc.status in (400, 401):
                raise ConnectorError(
                    "USPS did not accept the consumer key and secret.", status=exc.status
                ) from None
            raise
        token = str(answer.get("access_token", "")) if isinstance(answer, dict) else ""
        if not token:
            raise ConnectorError("USPS did not return an access token.")
        try:
            lifetime = int(answer.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        self.token, self.token_expires = token, time.monotonic() + max(lifetime - 60, 60)
        return token

    def track(self, numbers):
        """USPS's answer for up to 35 tracking numbers, getting a new token once if it expired."""
        body = [{"trackingNumber": number} for number in numbers]
        for renew in (False, True):
            response = self.client.request(
                "POST",
                "/tracking/v3r2/tracking",
                json=body,
                expected=(200, 207, 401, 403),
                headers={"Authorization": f"Bearer {self.access_token(renew=renew)}"},
            )
            if response.status_code != 401:
                break
        if response.status_code in (401, 403):
            raise ConnectorError(
                "USPS refused tracking. Check that the firm's app has the Tracking API and has "
                "accepted USPS's updated terms in the Business Portal.",
                status=response.status_code,
            )
        try:
            answer = response.json()
        except ValueError:
            raise ConnectorError("USPS sent a response that was not understood.") from None
        return answer if isinstance(answer, list) else []

    def record(self, number, item, checked_at):
        common = {
            "external_id": f"{number}@{checked_at:%Y%m%dT%H%M%S}",
            "occurred_at": checked_at,
            "carrier": CARRIER,
            "tracking_number": number,
        }
        if item is None or str(item.get("statusCode") or "200") not in ("200", ""):
            return ShipmentRecord(**common, problem=problem_of(item or {}))
        expected = item.get("deliveryDateExpectation") or {}
        return ShipmentRecord(
            **common,
            status=status_of(item),
            detail=str(item.get("statusSummary") or item.get("status") or "")[:255],
            delivered_at=delivered_at(item) if status_of(item) == "delivered" else None,
            expected_on=parse_day(
                expected.get("expectedDeliveryDate") or expected.get("predictedDeliveryDate")
            ),
        )

    def test_connection(self):
        self.access_token()

    def sync(self, capability, cursor):
        if capability != Capability.SHIPPING:
            raise ConnectorError(f"USPS does not sync {capability}.")
        numbers = lookups.due(Capability.SHIPPING, limit=PER_RUN, carrier=CARRIER)
        records = []
        for start in range(0, len(numbers), BATCH_SIZE):
            batch = numbers[start : start + BATCH_SIZE]
            answer = self.track(batch)
            checked_at = timezone.now()
            by_number = {
                str(item.get("trackingNumber") or "").upper(): item
                for item in answer
                if isinstance(item, dict)
            }
            records += [self.record(n, by_number.get(n), checked_at) for n in batch]
        return SyncBatch(records=records, cursor={})
