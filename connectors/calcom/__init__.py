"""Cal.com: consultations booked through Cal.com, hosted or self-hosted.

The firm connects with a Cal.com API key (``cal_…``) from the account whose event
types take consultations. For a self-hosted Cal.com, the connection takes the address
of its API v2 service. Turning on webhooks creates one webhook for that account's
bookings, reschedules, and cancellations, signed with a secret from this CRM.

Cal.com signs each delivery with ``X-Cal-Signature-256``: a hex HMAC-SHA256 of the
raw body. There is no signed timestamp header, so the CRM also refuses a delivery
whose ``createdAt`` is more than an hour off, and the framework files each event only
once. Unsigned deliveries are refused.

Only bookings of the event types the firm marks as consultations are filed. A booking
matches or creates the contact and makes the consult appointment. A reschedule is a
new booking that names the one it replaces, so the appointment moves. Cal.com sends no
tracking (UTM) values, so the lead source is not filled.

API reference: https://cal.com/docs/api-reference/v2/introduction
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient, check_url
from crm.connect.records import BookingRecord
from crm.connect.registry import register

HOSTED_API = "https://api.cal.com/v2"
SIGNATURE_HEADER = "X-Cal-Signature-256"
EVENT_TYPES_VERSION = "2026-06-12"
CREATED, RESCHEDULED, CANCELLED = "BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_CANCELLED"
TRIGGERS = (CREATED, RESCHEDULED, CANCELLED)
# Deliveries are refused when their createdAt is further off than this.
TOLERANCE = timedelta(hours=1)
# Booking questions Cal.com always asks; the rest are the firm's own and are kept.
STANDARD_FIELDS = {"name", "email", "attendeePhoneNumber", "location", "guests", "rescheduleReason"}
# A booker who gave only a phone number gets an address like this, which is not theirs.
PHONE_ONLY_DOMAIN = "@sms.cal.com"


def sign(body, secret):
    """Cal.com's signature for ``body``; used by tests and the setup guide."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def validate_api_url(value):
    try:
        check_url(value, allow_local_http=True)
    except ConnectorError:
        raise ValidationError(
            "Use the HTTPS address of Cal.com's API v2, or plain HTTP only on this computer or "
            "the firm's own network."
        ) from None
    if urlsplit(value).query:
        raise ValidationError("Leave out anything after a question mark.")


def event_type_options(connector):
    return connector.event_types()


def answer_text(value):
    if isinstance(value, dict):
        value = value.get("value", "")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value or "")


@register
class CalComConnector(Connector):
    key = "calcom"
    name = "Cal.com"
    description = (
        "File consultations booked through Cal.com, hosted or self-hosted: the contact is "
        "matched or created, the consult appointment is made, and reschedules and "
        "cancellations follow."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SCHEDULING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "Cal.com API key",
                help_text="Starts with cal_. Created in Cal.com under Settings > Developer > "
                "API keys, by the account whose event types take consultations.",
            ),
        ),
        docs_url="https://cal.com/docs/api-reference/v2/introduction",
        label="API key",
    )
    settings_fields = (
        SettingField(
            "api_url",
            "API address",
            default=HOSTED_API,
            help_text="Leave as it is for cal.com. For a self-hosted Cal.com, the address of "
            "its API v2 service, such as https://cal.example.test/api/v2.",
            validate=validate_api_url,
        ),
        SettingField(
            "consult_event_types",
            "Event types that are consultations",
            options=event_type_options,
            multiple=True,
            help_text="Only bookings of these event types are filed. Others are ignored.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=("When staff ask: this CRM's private webhook address and signing secret",),
        receives=(
            "Bookings, reschedules, and cancellations of the event types marked as "
            "consultations: the booker's name, email, phone, and answers to the booking "
            "questions, the time, the location, and the host's email",
            "The names of the account's Cal.com event types, to choose the consultations",
        ),
    )
    webhooks = WebhookSpec(
        capabilities=(Capability.SCHEDULING,),
        register_label="Create the webhook in Cal.com for this account's bookings, "
        "reschedules, and cancellations. Any webhook this connection made before is replaced.",
        instructions="For team event types, add the address and secret above as a webhook on "
        "each team event type in Cal.com, for Booking created, rescheduled, and cancelled. "
        "Cal.com does not retry a delivery the CRM did not accept, so keep the address "
        "reachable.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            self.setting("api_url") or HOSTED_API,
            name="Cal.com",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            },
            allow_local_http=True,
        )

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError(
                    "Cal.com did not accept the API key. Check that it is current.", status=401
                ) from None
            if exc.status == 404:
                raise ConnectorError(
                    "Cal.com's API was not found at that address. For a self-hosted Cal.com, "
                    "check the API address on the connection's page.",
                    status=404,
                ) from None
            raise

    def test_connection(self):
        me = self.call("GET", "/me").get("data") or {}
        if not me.get("id"):
            raise ConnectorError("Cal.com did not say whose key this is.")

    def event_types(self):
        """The account's event types, as (id, title) choices."""
        answer = self.call("GET", "/event-types", headers={"cal-api-version": EVENT_TYPES_VERSION})
        data = answer.get("data") or []
        if isinstance(data, dict):  # Older self-hosted versions group them.
            data = [
                event_type
                for group in data.get("eventTypeGroups") or []
                for event_type in group.get("eventTypes") or []
            ]
        return [
            (str(t["id"]), str(t.get("title") or t.get("slug") or t["id"]))
            for t in data
            if isinstance(t, dict) and t.get("id") is not None
        ]

    def register_webhook(self, url, secret):
        self.remove_webhook()
        made = self.call(
            "POST",
            "/webhooks",
            json={
                "subscriberUrl": url,
                "triggers": list(TRIGGERS),
                "active": True,
                "secret": secret,
                "version": "2021-10-20",
            },
        )
        webhook = str((made.get("data") or {}).get("id") or "")
        self.connection.config = {**(self.connection.config or {}), "webhook_id": webhook}
        self.connection.save(update_fields=["config", "updated_at"])
        return "Cal.com will send this account's bookings here."

    def remove_webhook(self):
        webhook = str((self.connection.config or {}).get("webhook_id", ""))
        if webhook:
            try:
                self.call("DELETE", f"/webhooks/{webhook}")
            except ConnectorError as exc:
                if exc.status != 404:
                    raise

    def revoke(self):
        self.remove_webhook()

    def verify_webhook(self, body, headers, secret, *, url=""):
        given = headers.get(SIGNATURE_HEADER, "")
        if not secret or not given or given == "no-secret-provided":
            return False
        if not hmac.compare_digest(given.lower(), sign(body, secret)):
            return False
        try:
            created = parse_time(json.loads(body).get("createdAt"))
        except (ValueError, AttributeError):
            return False
        return created is not None and abs(datetime.now(UTC) - created) <= TOLERANCE

    def parse_webhook(self, body, headers):
        try:
            event = json.loads(body)
        except ValueError:
            raise ConnectorError("The Cal.com event was not valid JSON.") from None
        if not isinstance(event, dict):
            raise ConnectorError("The Cal.com event was not understood.")
        trigger = event.get("triggerEvent")
        if trigger == "PING":
            # Cal.com's "Ping test" button: accepted, and nothing to file.
            return WebhookEnvelope(
                event_id=f"PING:{event.get('createdAt', '')}"[:255],
                capability=Capability.SCHEDULING,
                payload={"event": "PING"},
            )
        if trigger not in TRIGGERS:
            raise ConnectorError(
                "The Cal.com event was not a booking, reschedule, or cancellation."
            )
        booking = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not booking.get("uid"):
            raise ConnectorError("The Cal.com event had no booking.")
        attendee = (booking.get("attendees") or [{}])[0] or {}
        responses = booking.get("responses") if isinstance(booking.get("responses"), dict) else {}
        email = str(attendee.get("email") or "")
        if email.lower().endswith(PHONE_ONLY_DOMAIN):
            email = ""
        phone = answer_text(responses.get("attendeePhoneNumber")) or str(
            attendee.get("phoneNumber") or ""
        )
        video = str((booking.get("metadata") or {}).get("videoCallUrl") or "")
        location = video if video.startswith("https://") else str(booking.get("location") or "")
        if location.startswith("integrations:"):
            location = ""
        answers = {}
        for key, answer in responses.items():
            if key in STANDARD_FIELDS or not isinstance(answer, dict) or answer.get("isHidden"):
                continue
            text = answer_text(answer)
            if text:
                answers[str(answer.get("label") or key)[:200]] = text[:1000]
        payload = {
            "event": trigger,
            "uid": str(booking["uid"])[:200],
            "replaces": str(booking.get("rescheduleUid") or "")[:200]
            if trigger == RESCHEDULED
            else "",
            "event_type": str(booking.get("eventTypeId") or ""),
            "title": str(booking.get("title") or booking.get("eventTitle") or "Consultation")[:255],
            "starts_at": booking.get("startTime"),
            "ends_at": booking.get("endTime"),
            "location": location[:255],
            "name": str(attendee.get("name") or "")[:200],
            "email": email[:254],
            "phone": phone[:40],
            "answers": answers,
            "staff_email": str((booking.get("organizer") or {}).get("email") or "")[:254],
        }
        return WebhookEnvelope(
            event_id=f"{trigger}:{payload['uid']}"[:255],
            capability=Capability.SCHEDULING,
            payload=payload,
        )

    def handle_webhook(self, capability, payload):
        if payload["event"] == "PING":
            return []
        if payload["event_type"] not in self.setting("consult_event_types"):
            return []
        starts_at = parse_time(payload["starts_at"])
        if starts_at is None:
            raise ConnectorError("The Cal.com event had no start time.")
        return [
            BookingRecord(
                external_id=payload["uid"],
                occurred_at=starts_at,
                title=payload["title"],
                invitee_name=payload["name"],
                invitee_email=payload["email"],
                invitee_phone=payload["phone"],
                ends_at=parse_time(payload["ends_at"]),
                location=payload["location"],
                cancelled=payload["event"] == CANCELLED,
                answers=payload["answers"],
                replaces=payload["replaces"],
                staff_email=payload["staff_email"],
            )
        ]
