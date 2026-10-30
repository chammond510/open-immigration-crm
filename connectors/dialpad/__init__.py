"""Dialpad: calls and texts, with recording links and Dialpad's AI recap.

Events arrive by webhook. The connector creates the webhook through Dialpad's
API with a secret from this CRM, so Dialpad sends each event as a JWT signed with
that secret (HS256). Everything is read from the verified token; nothing outside
it is trusted. Company-wide subscriptions cover ended calls, recordings,
voicemails, AI recaps, and texts in both directions. A sync catches up on calls
the webhook missed. Dialpad has no API for past texts, so texts cannot be
backfilled. Staff send texts through the outbound policy, from their own
Dialpad number or a number the firm chooses.

API reference: https://developers.dialpad.com/reference
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

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
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient
from crm.connect.records import INBOUND, OUTBOUND, CallRecord, TextRecord
from crm.connect.registry import register

PRODUCTION, SANDBOX = "production", "sandbox"
API_URLS = {
    PRODUCTION: "https://dialpad.com/api/v2",
    SANDBOX: "https://sandbox.dialpad.com/api/v2",
}
# Dialpad allows 20 requests a second per company.
REQUEST_SPACING = 0.1
CALL_STATES = ("hangup", "recording", "voicemail_uploaded", "recap_summary")
BACKFILL_DAYS = 30
# Calls are listed only once they end, so each sync looks back over long calls.
SYNC_OVERLAP = timedelta(hours=6)
SYNC_LIMIT = 2000
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def validate_number(value):
    if not E164.match(value):
        raise ValidationError("Enter a Dialpad number in +12025550143 form, without spaces.")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def unb64url(data):
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def sign_jwt(claims, secret):
    """An HS256 JWT, as Dialpad sends events; used by tests and the setup guide."""
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    digest = hmac.new(secret.encode(), header + b"." + payload, hashlib.sha256).digest()
    return header + b"." + payload + b"." + b64url(digest)


def jwt_claims(token, secret):
    """The claims of an HS256 JWT signed with ``secret``, or None. Other algorithms fail."""
    # Only a trailing line break is tolerated; any other change fails.
    parts = token.rstrip(b"\r\n").split(b".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(unb64url(parts[0]))
        expected = hmac.new(secret.encode(), parts[0] + b"." + parts[1], hashlib.sha256).digest()
        if header.get("alg") != "HS256" or not hmac.compare_digest(unb64url(parts[2]), expected):
            return None
        claims = json.loads(unb64url(parts[1]))
    except (ValueError, AttributeError):
        return None
    return claims if isinstance(claims, dict) else None


def from_ms(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def recording_url(call):
    """The first recording link, in either of the forms Dialpad documents."""
    links = [
        item.get("url") for item in call.get("recording_details") or [] if isinstance(item, dict)
    ]
    links += [link for link in call.get("recording_url") or [] if isinstance(link, str)]
    return next((link for link in links if link and link.startswith("https://")), "")


def call_payload(call):
    """What processing needs from a Dialpad call, from an event or the call list."""
    target = call.get("target") if isinstance(call.get("target"), dict) else {}
    return {
        "type": "call",
        "id": str(call.get("call_id", "")),
        "direction": call.get("direction", ""),
        "remote": str(call.get("external_number") or "")[:40],
        "staff_email": str(target.get("email") or "")[:254],
        "started": call.get("date_started"),
        "duration_ms": call.get("duration"),
        "state": str(call.get("state") or "")[:40],
        "voicemail": bool(call.get("voicemail_link")),
        "recording_url": recording_url(call)[:1000],
        "recap": str(call.get("recap_summary") or "")[:4000],
    }


def call_record(payload):
    started = from_ms(payload.get("started"))
    if not payload["id"] or not payload["remote"] or started is None:
        return None
    if payload["direction"] not in (INBOUND, OUTBOUND):
        return None
    duration = payload.get("duration_ms")
    outcome = (
        "voicemail" if payload["voicemail"] else "missed" if payload["state"] == "missed" else ""
    )
    return CallRecord(
        external_id=payload["id"],
        occurred_at=started,
        direction=payload["direction"],
        remote=payload["remote"],
        duration_seconds=round(float(duration) / 1000) if duration else None,
        outcome=outcome,
        summary=payload["recap"],
        recording_url=payload["recording_url"],
        staff_email=payload["staff_email"],
    )


@register
class DialpadConnector(Connector):
    key = "dialpad"
    name = "Dialpad"
    description = (
        "File calls, with recording links and Dialpad's AI recap, and texts on the right "
        "contact automatically, and send texts from contact and matter pages."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.PHONE, Capability.TEXTING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "API key",
                help_text="A company admin creates it in Dialpad under Admin Settings > My "
                "Company > Authentication > API Keys, with the scopes calls:list, "
                "recordings_export, and message_content_export:all.",
            ),
        ),
        docs_url="https://developers.dialpad.com/docs/api-key-generation",
    )
    settings_fields = (
        SettingField(
            "environment",
            "Dialpad environment",
            choices=((PRODUCTION, "Production"), (SANDBOX, "Sandbox (a Dialpad test account)")),
            default=PRODUCTION,
        ),
        SettingField(
            "from_number",
            "Send texts from",
            required=False,
            help_text="Leave blank to send from each staff member's own Dialpad number, "
            "found by their email. Or enter one firm number, such as +12025550143.",
            validate=validate_number,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Texts staff send: the contact's phone number and the message",
            "The sending staff member's email, to find their Dialpad number",
            "When staff ask: this CRM's private webhook address and signing secret",
        ),
        receives=(
            "Calls: the other party's number, direction, time, length, recording link, "
            "and Dialpad's AI recap",
            "Texts in both directions: the other party's number and the message",
            "The staff member each call belongs to",
        ),
    )
    outbound_actions = {"send_text": Capability.TEXTING}
    sync_capabilities = frozenset({Capability.PHONE})
    sync_interval = timedelta(hours=1)
    webhooks = WebhookSpec(
        capabilities=(Capability.PHONE, Capability.TEXTING),
        register_label="Create the webhook in Dialpad, with company-wide subscriptions to "
        "ended calls, recordings, voicemails, AI recaps, and texts.",
        instructions="Dialpad webhooks are created through its API, which this CRM does when "
        "the box above is ticked. If that failed, check the API key's scopes and try again.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URLS.get(self.setting("environment"), API_URLS[PRODUCTION]),
            name="Dialpad",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            },
            min_interval=REQUEST_SPACING,
        )

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError("Dialpad did not accept the API key.", status=401) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Dialpad refused this request. Check the API key's scopes.", status=403
                ) from None
            raise

    def test_connection(self):
        self.call("GET", "/company")

    def sync(self, capability, cursor):
        now = timezone.now()
        after = from_ms((cursor or {}).get("after")) or now - timedelta(days=BACKFILL_DAYS)
        params = {
            "started_after": int((after - SYNC_OVERLAP).timestamp() * 1000),
            "started_before": int(now.timestamp() * 1000),
        }
        records, latest = [], after
        for call in self.client.paginate(
            "/call",
            items=lambda page: page.get("items") or [],
            next_request=lambda page: (
                ("/call", {**params, "cursor": page["cursor"]}) if page.get("cursor") else None
            ),
            params=params,
            limit=SYNC_LIMIT,
        ):
            record = call_record(call_payload(call))
            if record:
                records.append(record)
                latest = max(latest, record.occurred_at)
        return SyncBatch(records=records, cursor={"after": int(latest.timestamp() * 1000)})

    def sender(self, staff_email):
        """``from_number`` or ``user_id`` for sending: the firm's number, or the staff member's."""
        number = self.setting("from_number")
        if number:
            return {"from_number": number}
        users = self.call("GET", "/users", params={"email": staff_email}).get("items") or []
        if not staff_email or not users:
            raise ConnectorError(
                "No Dialpad user has your email address. Set a firm number to send from on "
                "the connection, or use your Dialpad email in the CRM."
            )
        return {"user_id": users[0]["id"]}

    def perform(self, action, payload):
        if action != "send_text":
            raise ConnectorError(f"Dialpad cannot {action}.")
        answer = self.call(
            "POST",
            "/sms",
            json={
                "to_numbers": [payload["to"]],
                "text": payload["body"],
                "infer_country_code": False,
                **self.sender(payload.get("staff_email", "")),
            },
        )
        if not answer.get("id") or answer.get("message_status") == "failed":
            raise ConnectorError("Dialpad did not send the text.")
        return str(answer["id"])

    def register_webhook(self, url, secret):
        self.remove_webhook()
        webhook = self.call("POST", "/webhooks", json={"hook_url": url, "secret": secret})
        endpoint = int(webhook["id"])
        config = {**self.connection.config, "webhook_id": endpoint}
        self.connection.config = config
        self.connection.save(update_fields=["config", "updated_at"])
        made = []
        if Capability.PHONE in self.connection.enabled_capabilities:
            self.call(
                "POST",
                "/subscriptions/call",
                json={"endpoint_id": endpoint, "call_states": list(CALL_STATES), "enabled": True},
            )
            made.append("calls")
        if Capability.TEXTING in self.connection.enabled_capabilities:
            self.call(
                "POST",
                "/subscriptions/sms",
                json={
                    "endpoint_id": endpoint,
                    "direction": "all",
                    "include_internal": False,
                    "status": True,
                    "enabled": True,
                },
            )
            made.append("texts")
        return f"created the webhook and subscribed it to {' and '.join(made)}."

    def remove_webhook(self):
        """Delete the webhook this connection created earlier; its subscriptions go with it."""
        endpoint = (self.connection.config or {}).get("webhook_id")
        if endpoint:
            try:
                self.call("DELETE", f"/webhooks/{int(endpoint)}")
            except ConnectorError as exc:
                if exc.status != 404:
                    raise

    def revoke(self):
        self.remove_webhook()

    def verify_webhook(self, body, headers, secret, *, url=""):
        return jwt_claims(body, secret) is not None

    def parse_webhook(self, body, headers):
        # Called after verify_webhook, so the token's signature is already checked.
        try:
            event = json.loads(unb64url(body.rstrip(b"\r\n").split(b".")[1]))
        except (IndexError, ValueError):
            raise ConnectorError("The Dialpad event was not a valid token.") from None
        if not isinstance(event, dict):
            raise ConnectorError("The Dialpad event was not understood.")
        if "call_id" in event:
            payload = call_payload(event)
            if not payload["id"]:
                raise ConnectorError("The Dialpad call event had no call ID.")
            stamp = event.get("event_timestamp", "")
            return WebhookEnvelope(
                event_id=f"call:{payload['id']}:{payload['state']}:{stamp}",
                capability=Capability.PHONE,
                payload=payload,
            )
        if event.get("id") and "from_number" in event:
            inbound = event.get("direction") == INBOUND
            to = event.get("to_number") or []
            payload = {
                "type": "text",
                "id": str(event["id"]),
                "direction": INBOUND if inbound else OUTBOUND,
                "remote": str(event.get("from_number") if inbound else (to[0] if to else ""))[:40],
                "body": str(event.get("text") or "")[:1600],
                "status": str(event.get("message_status") or "")[:20],
                "created": event.get("created_date"),
            }
            return WebhookEnvelope(
                event_id=f"sms:{payload['id']}:{payload['status']}",
                capability=Capability.TEXTING,
                payload=payload,
            )
        raise ConnectorError("The Dialpad event was neither a call nor a text.")

    def handle_webhook(self, capability, payload):
        if payload.get("type") == "call":
            record = call_record(payload)
            return [record] if record else []
        created = from_ms(payload.get("created")) or timezone.now()
        if not payload["remote"]:
            return []
        return [
            TextRecord(
                external_id=payload["id"],
                occurred_at=created,
                direction=payload["direction"],
                remote=payload["remote"],
                body=payload["body"],
                status=payload["status"],
            )
        ]
