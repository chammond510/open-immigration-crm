"""Calendly: consultations booked through the firm's Calendly organization.

The firm connects with a personal access token from a Calendly organization
admin. Turning on webhooks creates one organization webhook for bookings and
cancellations, signed with a secret from this CRM: each delivery carries
``Calendly-Webhook-Signature: t=<time>,v1=<HMAC-SHA256 of "<time>.<body>">`` and is
refused when the time is more than three minutes off.

Only bookings of the event types the firm marks as consultations, on the
connection's page, are filed; Calendly's event type names are never guessed
from. A booking matches or creates the contact and makes the consult
appointment. A reschedule moves the appointment rather than cancelling it.
Tracking values (UTM) can fill a contact field such as lead source.

API reference: https://developer.calendly.com/api-docs
"""

import hashlib
import hmac
import json
import time
from datetime import datetime

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
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient
from crm.connect.records import BookingRecord
from crm.connect.registry import register

API_URL = "https://api.calendly.com"
SIGNATURE_HEADER = "Calendly-Webhook-Signature"
TOLERANCE_SECONDS = 180
EVENTS = ("invitee.created", "invitee.canceled")
TRACKING_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def sign(body, secret, timestamp):
    """Calendly's signature header for ``body``; used by tests and the setup guide."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def event_type_options(connector):
    return connector.event_types()


def lead_source_options(connector):
    return [("", "Don't fill a field"), *lookups.options("lead_source_fields")]


@register
class CalendlyConnector(Connector):
    key = "calendly"
    name = "Calendly"
    description = (
        "File consultations booked through Calendly: the contact is matched or created, "
        "the consult appointment is made, and reschedules and cancellations follow."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SCHEDULING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "token",
                "Personal access token",
                help_text="Created by a Calendly organization owner or admin under Profile > "
                "Developer > Personal access tokens, with the scopes users:read, "
                "event_types:read, scheduled_events:read, and webhooks:write.",
            ),
        ),
        docs_url="https://developer.calendly.com/how-to-authenticate-with-personal-access-tokens",
        label="Personal access token",
    )
    settings_fields = (
        SettingField(
            "consult_event_types",
            "Event types that are consultations",
            options=event_type_options,
            multiple=True,
            help_text="Only bookings of these event types are filed. Others are ignored.",
        ),
        SettingField(
            "lead_source_field",
            "Fill this contact field for new contacts",
            options=lead_source_options,
            required=False,
            help_text="For example, the lead-source field the operating dashboard groups by. "
            "The booking link's tracking value must match one of its choices.",
        ),
        SettingField(
            "lead_source_from",
            "From this tracking value",
            choices=tuple((key, key) for key in ("utm_source", "utm_medium", "utm_campaign")),
            default="utm_source",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=("When staff ask: this CRM's private webhook address and signing secret",),
        receives=(
            "Bookings, reschedules, and cancellations of the event types marked as "
            "consultations: the invitee's name, email, phone, and answers to booking "
            "questions, the time, the location, and the booking link's tracking values",
            "The names of the firm's Calendly event types, to choose the consultations",
        ),
    )
    webhooks = WebhookSpec(
        capabilities=(Capability.SCHEDULING,),
        register_label="Create the organization webhook in Calendly for bookings and "
        "cancellations. Any webhook this connection made before is replaced.",
        instructions="Calendly webhooks are created through its API, which this CRM does when "
        "the box above is ticked. Webhooks need a paid Calendly plan.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="Calendly",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('token', '')}",
            },
        )
        self.organization = ""

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError("Calendly did not accept the token.", status=401) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Calendly refused the request. The token must belong to an organization "
                    "owner or admin and have the scopes listed on the connect page, and "
                    "webhooks need a paid plan.",
                    status=403,
                ) from None
            raise

    def org(self):
        if not self.organization:
            me = self.call("GET", "/users/me").get("resource") or {}
            self.organization = str(me.get("current_organization") or "")
            if not self.organization:
                raise ConnectorError("Calendly did not say which organization the token is in.")
        return self.organization

    def test_connection(self):
        self.org()

    def event_types(self):
        """The organization's event types, as (uri, name) choices."""
        params = {"organization": self.org(), "count": 100}

        def next_request(page):
            token = (page.get("pagination") or {}).get("next_page_token")
            return ("/event_types", {**params, "page_token": token}) if token else None

        types = self.client.paginate(
            "/event_types",
            items=lambda page: page.get("collection") or [],
            next_request=next_request,
            params=params,
            limit=500,
        )
        return [(t["uri"], t.get("name") or t["uri"]) for t in types if t.get("uri")]

    def register_webhook(self, url, secret):
        self.remove_webhook()
        made = self.call(
            "POST",
            "/webhook_subscriptions",
            json={
                "url": url,
                "events": list(EVENTS),
                "organization": self.org(),
                "scope": "organization",
                "signing_key": secret,
            },
        )
        webhook = str((made.get("resource") or {}).get("uri") or "")
        self.connection.config = {**(self.connection.config or {}), "webhook_uri": webhook}
        self.connection.save(update_fields=["config", "updated_at"])
        return "Calendly will send bookings and cancellations here."

    def remove_webhook(self):
        webhook = (self.connection.config or {}).get("webhook_uri", "")
        if webhook.startswith(f"{API_URL}/webhook_subscriptions/"):
            try:
                self.call("DELETE", webhook)
            except ConnectorError as exc:
                if exc.status != 404:
                    raise

    def revoke(self):
        self.remove_webhook()

    def verify_webhook(self, body, headers, secret, *, url=""):
        parts = dict(
            part.split("=", 1)
            for part in headers.get(SIGNATURE_HEADER, "").split(",")
            if "=" in part
        )
        stamp, given = parts.get("t", ""), parts.get("v1", "")
        if not stamp.isdigit() or not given:
            return False
        if abs(time.time() - int(stamp)) > TOLERANCE_SECONDS:
            return False
        return hmac.compare_digest(given, sign(body, secret, stamp).split("v1=", 1)[1])

    def parse_webhook(self, body, headers):
        try:
            event = json.loads(body)
        except ValueError:
            raise ConnectorError("The Calendly event was not valid JSON.") from None
        if not isinstance(event, dict) or event.get("event") not in EVENTS:
            raise ConnectorError("The Calendly event was not a booking or cancellation.")
        invitee = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        scheduled = (
            invitee.get("scheduled_event")
            if isinstance(invitee.get("scheduled_event"), dict)
            else {}
        )
        if not invitee.get("uri") or not scheduled:
            raise ConnectorError("The Calendly event had no invitee or scheduled event.")
        location = scheduled.get("location") or {}
        members = scheduled.get("event_memberships") or [{}]
        tracking = invitee.get("tracking") or {}
        payload = {
            "event": event["event"],
            "invitee": str(invitee["uri"]),
            "rescheduled": bool(invitee.get("rescheduled")),
            "old_invitee": str(invitee.get("old_invitee") or ""),
            "event_type": str(scheduled.get("event_type") or ""),
            "title": str(scheduled.get("name") or "Consultation")[:255],
            "starts_at": scheduled.get("start_time"),
            "ends_at": scheduled.get("end_time"),
            "location": str(location.get("location") or location.get("join_url") or "")[:255],
            "name": str(invitee.get("name") or "")[:200],
            "email": str(invitee.get("email") or "")[:254],
            "phone": str(invitee.get("text_reminder_number") or "")[:40],
            "answers": {
                str(qa.get("question", ""))[:200]: str(qa.get("answer", ""))[:1000]
                for qa in invitee.get("questions_and_answers") or []
                if isinstance(qa, dict)
            },
            "tracking": {
                k: str(tracking.get(k) or "")[:200] for k in TRACKING_KEYS if tracking.get(k)
            },
            "staff_email": str((members[0] or {}).get("user_email") or "")[:254],
        }
        return WebhookEnvelope(
            event_id=f"{payload['event']}:{payload['invitee']}"[:255],
            capability=Capability.SCHEDULING,
            payload=payload,
        )

    def handle_webhook(self, capability, payload):
        if payload["event_type"] not in self.setting("consult_event_types"):
            return []
        cancelled = payload["event"] == "invitee.canceled"
        if cancelled and payload["rescheduled"]:
            # The booking that replaces it moves the appointment instead.
            return []
        starts_at = parse_time(payload["starts_at"])
        if starts_at is None:
            raise ConnectorError("The Calendly event had no start time.")
        return [
            BookingRecord(
                external_id=payload["invitee"],
                occurred_at=starts_at,
                title=payload["title"],
                invitee_name=payload["name"],
                invitee_email=payload["email"],
                invitee_phone=payload["phone"],
                ends_at=parse_time(payload["ends_at"]),
                location=payload["location"],
                cancelled=cancelled,
                answers=payload["answers"],
                tracking=payload["tracking"],
                replaces=payload["old_invitee"],
                staff_email=payload["staff_email"],
            )
        ]
