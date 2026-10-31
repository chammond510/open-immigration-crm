"""UPS: tracking for the packages a firm ships, through the UPS Tracking API.

The firm creates an app in the UPS Developer Portal, tied to its UPS shipper account,
with the Tracking product, and enters the app's client ID and secret. Each hour, the
worker asks core for the UPS tracking numbers due for a check and looks them up one at
a time, as the API requires, spaced a second apart and at most 40 a run, to stay well
inside UPS's monthly quota. It signs in with the client credentials grant, reusing the
token until it nearly expires. It reports each package's status in the CRM's terms,
UPS's own description and where the latest scan was, the delivery time, and the
scheduled delivery date.

UPS's API Access Agreement limits keeping its tracking data to nine months after a
request; core clears a carrier's own words from a shipment nine months after its
last check.

API reference: https://developer.ups.com/tag/Tracking
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

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
from crm.connect.records import NOT_FOUND, UNAVAILABLE, ShipmentRecord
from crm.connect.registry import register

API_URL = "https://onlinetools.ups.com"
CARRIER = "ups"
TRANSACTION_SOURCE = "open-immigration-crm"
PER_RUN = 40
MIN_INTERVAL = 1.0
# UPS's status codes, as numbers since UPS pads them with zeros.
OUT_FOR_DELIVERY = {6, 21, 37, 89, 91, 146, 162}
RETURNED = {33, 34, 35, 36, 67, 118, 119}


def code_of(status):
    try:
        return int(str((status or {}).get("statusCode") or "").strip())
    except ValueError:
        return None


def status_of(package):
    """The CRM's status for one package, from UPS's activity type and status code."""
    current = package.get("currentStatus") or {}
    kind = str(current.get("type") or "").upper()
    code = code_of(current)
    if code in RETURNED:
        return "returned"
    if kind == "D":
        return "delivered"
    if kind in ("X", "MV"):
        return "problem"
    if kind == "M":
        return "sent"
    if code in OUT_FOR_DELIVERY:
        return "out_for_delivery"
    return "in_transit"


def gmt_time(activity):
    try:
        moment = datetime.strptime(
            f"{activity.get('gmtDate', '')}{activity.get('gmtTime', '')}", "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC)


def day_of(value):
    try:
        return datetime.strptime(str(value or ""), "%Y%m%d").date()
    except ValueError:
        return None


def delivered_at(package):
    for activity in package.get("activity") or []:
        if str((activity.get("status") or {}).get("type") or "").upper() == "D":
            return gmt_time(activity)
    return None


def expected_on(package):
    for kind in ("RDD", "SDD"):
        for item in package.get("deliveryDate") or []:
            if item.get("type") == kind:
                found = day_of(item.get("date"))
                if found:
                    return found
    return None


def detail_of(package):
    """UPS's description of the current status, and where the latest scan was."""
    current = package.get("currentStatus") or {}
    activities = package.get("activity") or [{}]
    address = (activities[0].get("location") or {}).get("address") or {}
    parts = (current.get("description"), address.get("city"), address.get("stateProvince"))
    return ", ".join(str(part).strip() for part in parts if part)


@register
class UpsConnector(Connector):
    key = "ups"
    name = "UPS"
    description = (
        "Track filing packages sent by UPS and note their delivery, or any problem, on the matter."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SHIPPING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField("client_id", "Client ID", secret=False),
            CredentialField("client_secret", "Client secret"),
        ),
        docs_url="https://developer.ups.com/get-started",
        label="App credentials",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The firm's UPS client ID and secret, to get an access token",
            "The tracking numbers of the firm's UPS shipments still on their way, about every "
            "four hours each",
        ),
        receives=(
            "Each package's status, UPS's description of it and where the latest scan was, "
            "when it was delivered, and when it is scheduled",
        ),
    )
    sync_capabilities = frozenset({Capability.SHIPPING})
    # Each run checks only the shipments due, so most hourly runs send little or nothing.
    sync_interval = timedelta(hours=1)

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="UPS",
            headers={"Accept": "application/json"},
            min_interval=MIN_INTERVAL,
        )
        # UPS takes the client ID and secret for a token by HTTP Basic.
        self.signer = ProviderClient(
            API_URL,
            name="UPS",
            auth=(
                str(self.credentials.get("client_id", "")),
                str(self.credentials.get("client_secret", "")),
            ),
        )
        self.token = ""
        self.token_expires = 0.0

    def access_token(self, *, renew=False):
        """A bearer token from the client credentials grant, reused until it nearly expires."""
        if self.token and not renew and time.monotonic() < self.token_expires:
            return self.token
        try:
            answer = self.signer.post(
                "/security/v1/oauth/token", data={"grant_type": "client_credentials"}
            )
        except ConnectorError as exc:
            if exc.status in (400, 401):
                raise ConnectorError(
                    "UPS did not accept the client ID and secret.", status=exc.status
                ) from None
            raise
        token = str(answer.get("access_token", "")) if isinstance(answer, dict) else ""
        if not token:
            raise ConnectorError("UPS did not return an access token.")
        try:
            lifetime = int(answer.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        self.token, self.token_expires = token, time.monotonic() + max(lifetime - 60, 60)
        return token

    def lookup(self, number):
        """UPS's package for one tracking number, or None when UPS has no record of it."""
        for renew in (False, True):
            response = self.client.request(
                "GET",
                f"/api/track/v1/details/{number}",
                params={"locale": "en_US"},
                expected=(200, 401, 403, 404),
                headers={
                    "Authorization": f"Bearer {self.access_token(renew=renew)}",
                    "transId": uuid.uuid4().hex,
                    "transactionSrc": TRANSACTION_SOURCE,
                },
            )
            if response.status_code != 401:
                break
        if response.status_code in (401, 403):
            raise ConnectorError(
                "UPS refused tracking. Check that the firm's app includes the Tracking API and "
                "is not blocked.",
                status=response.status_code,
            )
        if response.status_code == 404:
            return None
        try:
            answer = response.json()
        except ValueError:
            raise ConnectorError("UPS sent a response that was not understood.") from None
        for shipment in (answer.get("trackResponse") or {}).get("shipment") or []:
            for package in shipment.get("package") or []:
                return package
        return None

    def record(self, number, checked_at):
        common = {
            "external_id": f"{number}@{checked_at:%Y%m%dT%H%M%S}",
            "occurred_at": checked_at,
            "carrier": CARRIER,
            "tracking_number": number,
        }
        try:
            package = self.lookup(number)
        except ConnectorError as exc:
            if exc.status in (401, 403):
                raise
            return ShipmentRecord(**common, problem=UNAVAILABLE)
        if package is None:
            return ShipmentRecord(**common, problem=NOT_FOUND)
        status = status_of(package)
        return ShipmentRecord(
            **common,
            status=status,
            detail=detail_of(package)[:255],
            delivered_at=delivered_at(package) if status == "delivered" else None,
            expected_on=expected_on(package),
        )

    def test_connection(self):
        self.access_token()

    def sync(self, capability, cursor):
        if capability != Capability.SHIPPING:
            raise ConnectorError(f"UPS does not sync {capability}.")
        numbers = lookups.due(Capability.SHIPPING, limit=PER_RUN, carrier=CARRIER)
        return SyncBatch(
            records=[self.record(number, timezone.now()) for number in numbers], cursor={}
        )
