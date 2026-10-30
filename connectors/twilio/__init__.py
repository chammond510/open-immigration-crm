"""Twilio Programmable Messaging: texts from the firm's Twilio number or Messaging Service.

Staff send texts through the outbound policy. Incoming texts and delivery
updates arrive by webhook, each signed by Twilio with the account's Auth Token
over the exact address it was sent to (``X-Twilio-Signature``). The webhook answers
with empty TwiML, so Twilio sends no reply of its own. When asked, the connector
sets that address on the Twilio number or Messaging Service; it then also asks
for delivery updates on each text it sends.

Message API: https://www.twilio.com/docs/messaging/api/message-resource
Signatures: https://www.twilio.com/docs/usage/webhooks/webhooks-security
"""

import base64
import hashlib
import hmac
import re
from datetime import datetime
from urllib.parse import parse_qs

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
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient
from crm.connect.records import INBOUND, OUTBOUND, TextRecord
from crm.connect.registry import register

API_URL = "https://api.twilio.com/2010-04-01"
MESSAGING_URL = "https://messaging.twilio.com/v1"
SIGNATURE_HEADER = "X-Twilio-Signature"
ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
SERVICE_SID = re.compile(r"^MG[0-9a-fA-F]{32}$")
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
OPT_OUT_TYPES = {"STOP": "stop", "START": "start"}
# Twilio error codes for a refused text, explained without the numbers involved.
REFUSALS = {
    20003: "Twilio did not accept the Account SID and Auth Token.",
    21211: "Twilio says the contact's phone number is not valid.",
    21408: "The Twilio account is not allowed to text that country.",
    21606: "The sender is not a Twilio number that can send texts.",
    21610: "The contact replied STOP to the firm's Twilio number, so Twilio will not "
    "text them until they reply START.",
    21612: "Twilio cannot send texts between those two numbers.",
    21614: "Twilio says the contact's number cannot receive texts.",
    21659: "The sender is not a Twilio number in this account.",
}


def validate_sender(value):
    if not (E164.match(value) or SERVICE_SID.match(value)):
        raise ValidationError(
            "Enter the Twilio number in +1 202 555 0143 form without spaces, "
            "or a Messaging Service SID starting with MG."
        )


def form_params(body):
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None


def signature(url, params, auth_token):
    """Twilio's signature: the URL, then each parameter's name and value, sorted by name."""
    data = url
    for name in sorted(params):
        for value in sorted(set(params[name])):
            data += name + value
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@register
class TwilioConnector(Connector):
    key = "twilio"
    name = "Twilio"
    description = (
        "Send texts from contact and matter pages through the firm's Twilio number or "
        "Messaging Service, and file incoming texts and STOP replies automatically."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TEXTING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "account_sid",
                "Account SID",
                secret=False,
                help_text="Starts with AC. On the Twilio Console's account dashboard.",
            ),
            CredentialField(
                "auth_token",
                "Auth Token",
                help_text="Twilio also signs the events it sends this CRM with it.",
            ),
        ),
        docs_url="https://www.twilio.com/docs/iam/api/authtoken",
        label="Account SID and Auth Token",
    )
    settings_fields = (
        SettingField(
            "sender",
            "Send from",
            help_text="The firm's Twilio number, such as +12025550143, or a Messaging "
            "Service SID starting with MG.",
            validate=validate_sender,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Texts staff send: the contact's phone number and the message",
            "When staff ask: this CRM's private webhook address, set on the Twilio number "
            "or Messaging Service",
        ),
        receives=(
            "Texts sent to the firm's Twilio number: the sender's number and the message",
            "Delivery updates for texts sent from this CRM",
            "Twilio's reading of STOP and START replies",
        ),
    )
    outbound_actions = {"send_text": Capability.TEXTING}
    webhooks = WebhookSpec(
        capabilities=(Capability.TEXTING,),
        secret_source="credential",
        secret_credential="auth_token",
        register_label="Set this address in Twilio for incoming texts and delivery updates. "
        "It replaces the address now set on the number or Messaging Service.",
        instructions="To set it by hand instead: in the Twilio Console, open the number (or "
        "the Messaging Service's Integration settings) and paste this address as the "
        "incoming message webhook, using HTTP POST.",
        ack_body=EMPTY_TWIML,
        ack_content_type="text/xml",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="Twilio",
            headers={"Accept": "application/json"},
            auth=(self.credentials.get("account_sid", ""), self.credentials.get("auth_token", "")),
        )

    @property
    def account_path(self):
        sid = self.credentials.get("account_sid", "")
        if not ACCOUNT_SID.match(sid):
            raise ConnectorError("The Account SID should start with AC and have 34 characters.")
        return f"/Accounts/{sid}"

    @property
    def sender(self):
        return self.setting("sender")

    def call(self, method, path, **kwargs):
        """A Twilio API answer, with Twilio's refusals explained by error code."""
        response = self.client.request(
            method, path, expected=(200, 201, 204, 400, 401, 403, 404), **kwargs
        )
        try:
            answer = response.json() if response.content else {}
        except ValueError:
            answer = {}
        if response.status_code < 400:
            return answer
        code = answer.get("code") if isinstance(answer, dict) else None
        if response.status_code == 401:
            code = 20003
        message = REFUSALS.get(code) or (
            f"Twilio refused the request (error {code})."
            if code
            else f"Twilio answered HTTP {response.status_code}."
        )
        raise ConnectorError(message, status=response.status_code)

    def number_sid(self):
        numbers = (
            self.call(
                "GET",
                f"{self.account_path}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": self.sender},
            ).get("incoming_phone_numbers")
            or []
        )
        if not numbers:
            raise ConnectorError("The sender number is not in this Twilio account.")
        return numbers[0]["sid"]

    def test_connection(self):
        self.call("GET", f"{self.account_path}.json")
        if SERVICE_SID.match(self.sender):
            self.call("GET", f"{MESSAGING_URL}/Services/{self.sender}")
        else:
            self.number_sid()

    def perform(self, action, payload):
        if action != "send_text":
            raise ConnectorError(f"Twilio cannot {action}.")
        data = {"To": payload["to"], "Body": payload["body"]}
        data["MessagingServiceSid" if SERVICE_SID.match(self.sender) else "From"] = self.sender
        if self.credentials.get("webhook_url"):
            data["StatusCallback"] = self.credentials["webhook_url"]
        answer = self.call("POST", f"{self.account_path}/Messages.json", data=data)
        if not answer.get("sid"):
            raise ConnectorError("Twilio did not return a message ID.")
        return answer["sid"]

    def register_webhook(self, url, secret):
        if SERVICE_SID.match(self.sender):
            self.call(
                "POST",
                f"{MESSAGING_URL}/Services/{self.sender}",
                data={
                    "InboundRequestUrl": url,
                    "InboundMethod": "POST",
                    "StatusCallback": url,
                    "UseInboundWebhookOnNumber": "false",
                },
            )
        else:
            self.call(
                "POST",
                f"{self.account_path}/IncomingPhoneNumbers/{self.number_sid()}.json",
                data={"SmsUrl": url, "SmsMethod": "POST"},
            )
        # Kept encrypted with the other credentials, to ask for delivery updates.
        self.connection.credentials = {**self.credentials, "webhook_url": url}
        self.connection.save(update_fields=["credentials", "updated_at"])
        return f"texts to {self.sender} and their delivery updates now come to this CRM."

    def verify_webhook(self, body, headers, secret, *, url=""):
        given = headers.get(SIGNATURE_HEADER, "")
        params = form_params(body)
        if not given or params is None:
            return False
        addresses = {url, self.credentials.get("webhook_url", "")} - {""}
        return any(
            hmac.compare_digest(given, signature(address, params, secret)) for address in addresses
        )

    def parse_webhook(self, body, headers):
        params = {name: values[0] for name, values in (form_params(body) or {}).items()}
        sid = params.get("MessageSid") or params.get("SmsSid") or ""
        if not sid:
            raise ConnectorError("The Twilio event had no message ID.")
        if params.get("AccountSid") != self.credentials.get("account_sid"):
            raise ConnectorError("The Twilio event came from a different account.")
        # Incoming texts carry no MessageStatus; delivery updates always do.
        inbound = "MessageStatus" not in params
        status = params.get("SmsStatus", "") if inbound else params["MessageStatus"]
        payload = {
            "sid": sid,
            "direction": INBOUND if inbound else OUTBOUND,
            "remote": params.get("From" if inbound else "To", "")[:40],
            "body": params.get("Body", "")[:1600] if inbound else "",
            "status": status[:20],
            "opt_out": OPT_OUT_TYPES.get(params.get("OptOutType", "").upper(), ""),
            "received_at": timezone.now().isoformat(),
        }
        # Each delivery update is its own event; Twilio retries repeat the same one.
        event_id = sid if inbound else f"{sid}:{status}"
        return WebhookEnvelope(event_id=event_id, capability=Capability.TEXTING, payload=payload)

    def handle_webhook(self, capability, payload):
        return [
            TextRecord(
                external_id=payload["sid"],
                occurred_at=datetime.fromisoformat(payload["received_at"]),
                direction=payload["direction"],
                remote=payload["remote"],
                body=payload["body"],
                status=payload["status"],
                opt_out=payload["opt_out"],
            )
        ]
