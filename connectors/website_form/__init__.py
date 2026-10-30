"""Website form: submissions from the firm's website become leads in the CRM.

The firm's website, or a tool between the website and the CRM such as n8n, Zapier,
Make, or a WordPress plugin, sends each submission as JSON to the connection's
private address, signed with the Standard Webhooks scheme: ``webhook-id``,
``webhook-timestamp``, and ``webhook-signature`` (``v1,`` and a base64 HMAC-SHA256 of
``"{id}.{timestamp}.{body}"`` with the connection's ``whsec_`` secret). The CRM refuses a
submission that is unsigned, wrongly signed, or more than five minutes old, and files
each ``webhook-id`` only once, so a captured submission cannot be replayed.

A browser cannot sign a submission without revealing the secret, so the signing
happens on the website's server or in the tool between. The CRM never answers with
anything but whether it accepted the submission.

The body names the person (``name``, or ``first_name`` and ``last_name``), with
``email`` or ``phone``, and may have ``message``, ``form`` (which form it came from),
``source`` and ``utm_*`` tracking values, and ``answers``: other questions and their
answers. Any other plain value at the top level is kept as an answer too.

Standard Webhooks: https://www.standardwebhooks.com
"""

import hmac
import json
import time
from datetime import UTC, datetime

from connectors.webhooks import new_secret, sign
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
from crm.connect.records import LeadRecord
from crm.connect.registry import register

TOLERANCE_SECONDS = 300
TRACKING_KEYS = ("source", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
PERSON_KEYS = ("name", "first_name", "last_name", "email", "phone", "message", "form", "answers")
MAX_ANSWERS = 50


def lead_source_options(connector):
    return [("", "Don't fill a field"), *lookups.options("lead_source_fields")]


def text(value, limit):
    if isinstance(value, bool) or value is None:
        return "" if value is None else ("Yes" if value else "No")
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if not isinstance(item, (dict, list)))
    if isinstance(value, dict):
        return ""
    return " ".join(str(value).split())[:limit]


@register
class WebsiteFormConnector(Connector):
    key = "website_form"
    name = "Website form"
    description = (
        "Turn submissions from the firm's website form into new leads, with the lead source "
        "and the answers kept on the contact. Submissions must be signed."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.LEAD_CAPTURE})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "signing_secret",
                "Signing secret",
                generate=new_secret,
                help_text="Copy this into the website's form handler, or the tool that "
                "sends submissions, to sign each one. It is not shown again.",
            ),
        ),
        docs_url="https://www.standardwebhooks.com",
        label="Signed webhook",
    )
    settings_fields = (
        SettingField(
            "lead_source_field",
            "Fill this contact field for new leads",
            options=lead_source_options,
            required=False,
            help_text="For example, the lead-source field the operating dashboard groups by. "
            "The submission's value must match one of its choices.",
        ),
        SettingField(
            "lead_source_from",
            "From this value in the submission",
            choices=tuple(
                (key, key) for key in ("source", "utm_source", "utm_medium", "utm_campaign")
            ),
            default="source",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=("Only whether each submission was accepted",),
        receives=(
            "Each submission from the website form: the person's name, email, phone, message, "
            "and other answers, which form it came from, and its source and tracking values",
        ),
    )
    webhooks = WebhookSpec(
        capabilities=(Capability.LEAD_CAPTURE,),
        secret_source="credential",
        secret_credential="signing_secret",
        instructions="Give this address to the website's form handler, or the tool that "
        "sends submissions, with the signing secret shown when the connection was made. "
        "Each submission must be signed with the Standard Webhooks scheme; the setup guide "
        "has examples.",
    )

    def test_connection(self):
        if not str(self.credentials.get("signing_secret", "")).startswith("whsec_"):
            raise ConnectorError("Connect again to make a new signing secret.")

    def verify_webhook(self, body, headers, secret, *, url=""):
        message_id = headers.get("webhook-id", "")
        stamp = headers.get("webhook-timestamp", "")
        given = headers.get("webhook-signature", "").split()
        if not message_id or not stamp.isdigit() or not given or not secret:
            return False
        if abs(time.time() - int(stamp)) > TOLERANCE_SECONDS:
            return False
        expected = sign(secret, message_id, stamp, body)
        return any(hmac.compare_digest(candidate, expected) for candidate in given)

    def parse_webhook(self, body, headers):
        try:
            found = json.loads(body)
        except ValueError:
            raise ConnectorError("The submission was not JSON.") from None
        if not isinstance(found, dict):
            raise ConnectorError("The submission was not a JSON object.")
        fields = {str(k).strip().lower(): v for k, v in found.items()}
        answers = {}
        nested = fields.get("answers") if isinstance(fields.get("answers"), dict) else {}
        extra = {
            k: v
            for k, v in found.items()
            if str(k).strip().lower() not in (*PERSON_KEYS, *TRACKING_KEYS)
        }
        for question, answer in [*nested.items(), *extra.items()]:
            value = text(answer, 1000)
            if value and len(answers) < MAX_ANSWERS:
                answers[text(question, 200)] = value
        payload = {
            "id": text(headers.get("webhook-id", ""), 200),
            "sent_at": int(headers.get("webhook-timestamp") or time.time()),
            "name": text(fields.get("name"), 200),
            "first_name": text(fields.get("first_name"), 100),
            "last_name": text(fields.get("last_name"), 100),
            "email": text(fields.get("email"), 254),
            "phone": text(fields.get("phone"), 40),
            "message": str(fields.get("message") or "")[:5000],
            "form": text(fields.get("form"), 200),
            "answers": answers,
            "tracking": {
                k: text(fields[k], 200) for k in TRACKING_KEYS if text(fields.get(k), 200)
            },
        }
        if not (payload["email"] or payload["phone"]):
            raise ConnectorError("The submission had no email or phone.")
        return WebhookEnvelope(
            event_id=payload["id"], capability=Capability.LEAD_CAPTURE, payload=payload
        )

    def handle_webhook(self, capability, payload):
        record = LeadRecord(
            external_id=payload["id"],
            occurred_at=datetime.fromtimestamp(payload["sent_at"], UTC),
            name=payload["name"],
            first_name=payload["first_name"],
            last_name=payload["last_name"],
            email=payload["email"],
            phone=payload["phone"],
            message=payload["message"],
            form=payload["form"],
            answers=payload["answers"],
            tracking=payload["tracking"],
        )
        if record.problems():
            raise ConnectorError("The submission had no name, or no email or phone.")
        return [record]
