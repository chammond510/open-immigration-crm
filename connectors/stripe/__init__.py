"""Stripe: payment links for invoices, and payments recorded from signed webhooks.

The firm connects with a restricted API key (``rk_…``) holding only the permissions
the setup guide lists; full secret keys are refused. One Stripe account pays out to
one bank account, so each connection is set to the account its payouts reach:
operating, the default, or trust. Core asks it for links into that account only.

Stripe takes its fees out of each payment before paying out, so a payout into a trust
account arrives short of what the client paid. Most state bars do not allow fees to
come out of trust money; the setup guide asks the firm to check its own rules before
choosing trust.

Links: one Payment Link per invoice account, with an inline price for the amount and
the invoice number and account in its metadata, which Stripe copies to the checkout
and to the payment. The payment methods offered are the firm's choice in Stripe.
When a link is paid, the connector deactivates it, and reactivates it if a bank
payment later fails, so a client cannot pay the same link twice.

Payments: webhooks for checkout completion, bank payments that clear or fail later,
and refunds, each signed with ``Stripe-Signature`` and refused when more than five
minutes old. Only payments made through this CRM's links are recorded; the firm's
other Stripe activity is ignored.

API reference: https://docs.stripe.com/api
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

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
from crm.connect.records import PaymentRecord
from crm.connect.registry import register

API_URL = "https://api.stripe.com/v1"
API_VERSION = "2026-10-28.endive"
SIGNATURE_HEADER = "Stripe-Signature"
TOLERANCE_SECONDS = 300
COMPLETED = "checkout.session.completed"
CLEARED = "checkout.session.async_payment_succeeded"
FAILED = "checkout.session.async_payment_failed"
REFUNDED = "charge.refunded"
EVENTS = (COMPLETED, CLEARED, FAILED, REFUNDED)
# Metadata marking links this CRM made, so other Stripe payments are left alone.
INVOICE_KEY, ACCOUNT_KEY = "crm_invoice", "crm_deposit_account"
ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "vnd", "vuv", "xaf", "xof", "xpf",
}  # fmt: skip


def minor_units(amount, currency):
    factor = 1 if currency.lower() in ZERO_DECIMAL else 100
    return int((Decimal(amount) * factor).quantize(Decimal("1")))


def major_units(amount, currency):
    factor = 1 if currency.lower() in ZERO_DECIMAL else 100
    return str((Decimal(int(amount or 0)) / factor).quantize(Decimal("0.01")))


def sign(body, secret, timestamp):
    """Stripe's signature header for ``body``; used by tests."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def check_key(key):
    if str(key or "").startswith(("sk_live_", "sk_test_", "sk_org_")):
        raise ConnectorError(
            "Use a restricted key (rk_…) with only the permissions the setup guide lists, "
            "not the account's secret key."
        )
    if not str(key or "").startswith(("rk_live_", "rk_test_")):
        raise ConnectorError("A Stripe restricted key starts with rk_live_ or rk_test_.")


@register
class StripeConnector(Connector):
    key = "stripe"
    name = "Stripe"
    description = (
        "Send clients Stripe payment links for invoices, and record their payments and "
        "refunds on the invoice. Each Stripe account pays into one bank account."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.PAYMENTS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "Restricted API key",
                help_text="Starts with rk_live_. Create it in Stripe under Developers > API "
                "keys > Create restricted key, with the permissions in the setup guide. The "
                "account's secret key (sk_) is not accepted.",
            ),
        ),
        docs_url="https://docs.stripe.com/keys/restricted-api-keys",
        label="Restricted API key",
    )
    settings_fields = (
        SettingField(
            "deposit_account",
            "This Stripe account pays out to",
            choices=(
                ("operating", "The operating account"),
                ("trust", "The trust account (read the setup guide first)"),
            ),
            default="operating",
            help_text="Stripe takes its fees out of each payment before paying out, so a "
            "trust payout arrives short of what the client paid. Most state bars do not allow "
            "that; check your rules before choosing trust.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "For each payment link: the invoice number, the amount, and which account it is "
            "for, and the client's email to fill in on Stripe's page",
            "When staff ask: this CRM's private webhook address",
        ),
        receives=(
            "Payments and refunds made through this CRM's links: the amount, the invoice "
            "number, the account, and the payer's email",
        ),
    )
    outbound_actions = {"create_payment_link": Capability.PAYMENTS}
    webhooks = WebhookSpec(
        capabilities=(Capability.PAYMENTS,),
        secret_source="provider",
        register_label="Create the webhook endpoint in Stripe for payments and refunds, and "
        "keep the signing secret Stripe returns. Any endpoint this connection made before is "
        "removed.",
        instructions="To set it up by hand instead, add an endpoint in Stripe under Developers "
        "> Webhooks with this address, for the events checkout.session.completed, "
        "checkout.session.async_payment_succeeded, checkout.session.async_payment_failed, "
        "and charge.refunded, then paste its signing secret on the connection's page.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="Stripe",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
                "Stripe-Version": API_VERSION,
            },
        )

    def call(self, method, path, **kwargs):
        check_key(self.credentials.get("api_key", ""))
        if method == "POST":
            # A retried request must not make a second link or endpoint.
            kwargs.setdefault("headers", {})["Idempotency-Key"] = str(uuid.uuid4())
        response = self.client.request(
            method, path, expected=(200, 400, 401, 402, 403, 404), **kwargs
        )
        try:
            answer = response.json()
        except ValueError:
            raise ConnectorError("Stripe's answer was not understood.") from None
        if response.status_code == 200:
            return answer
        error = answer.get("error") if isinstance(answer, dict) else None
        message = str((error or {}).get("message") or "")[:300]
        if response.status_code == 401:
            raise ConnectorError("Stripe did not accept the API key.", status=401)
        if response.status_code == 403:
            raise ConnectorError(
                f"The restricted key is missing a permission this needs. {message}".strip(),
                status=403,
            )
        raise ConnectorError(
            f"Stripe refused the request. {message}".strip(), status=response.status_code
        )

    def deposit_accounts(self):
        return (self.setting("deposit_account") or "operating",)

    def test_connection(self):
        self.call("GET", "/payment_links", params={"limit": 1})

    # Payment links

    def perform(self, action, payload):
        if action != "create_payment_link":
            raise ConnectorError(f"Stripe cannot {action}.")
        account = payload["deposit_account"]
        if account not in self.deposit_accounts():
            raise ConnectorError(f"This Stripe account does not pay into the {account} account.")
        currency = str(payload.get("currency") or "USD").lower()
        invoice = str(payload["invoice_number"])
        metadata = {INVOICE_KEY: invoice, ACCOUNT_KEY: account}
        data = {
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": minor_units(payload["amount"], currency),
            "line_items[0][price_data][product_data][name]": f"Invoice {invoice}",
            "line_items[0][quantity]": 1,
            "submit_type": "pay",
            "payment_intent_data[description]": f"Invoice {invoice}",
            "inactive_message": "This payment link has been paid or replaced. Contact the firm "
            "for a current link.",
        }
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = value
            data[f"payment_intent_data[metadata][{key}]"] = value
        link = self.call("POST", "/payment_links", data=data)
        url = str(link.get("url") or "")
        if not url.startswith("https://"):
            raise ConnectorError("Stripe did not return a payment link.")
        email = str(payload.get("email") or "")
        return f"{url}?prefilled_email={quote(email)}" if email else url

    def set_link_active(self, link_id, active):
        if link_id:
            self.call("POST", f"/payment_links/{link_id}", data={"active": str(active).lower()})

    # Webhooks

    def register_webhook(self, url, secret):
        self.remove_webhook()
        made = self.call(
            "POST",
            "/webhook_endpoints",
            data={
                "url": url,
                "enabled_events[]": list(EVENTS),
                "api_version": API_VERSION,
                "description": "Open Immigration CRM payments",
            },
        )
        if not str(made.get("secret") or "").startswith("whsec_"):
            raise ConnectorError("Stripe did not return the endpoint's signing secret.")
        self.connection.config = {**(self.connection.config or {}), "webhook_id": made["id"]}
        self.connection.webhook_secret = made["secret"]
        self.connection.save(update_fields=["config", "webhook_secret", "updated_at"])
        return "Stripe will send payments and refunds here, signed with its own secret."

    def remove_webhook(self):
        endpoint = str((self.connection.config or {}).get("webhook_id", ""))
        if endpoint.startswith("we_"):
            try:
                self.call("DELETE", f"/webhook_endpoints/{endpoint}")
            except ConnectorError as exc:
                if exc.status != 404:
                    raise

    def revoke(self):
        self.remove_webhook()

    def verify_webhook(self, body, headers, secret, *, url=""):
        stamp, signatures = "", []
        for part in headers.get(SIGNATURE_HEADER, "").split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                stamp = value
            elif key == "v1":
                signatures.append(value)
        if not stamp.isdigit() or not signatures or not secret:
            return False
        if abs(time.time() - int(stamp)) > TOLERANCE_SECONDS:
            return False
        expected = sign(body, secret, stamp).split("v1=", 1)[1]
        return any(hmac.compare_digest(given, expected) for given in signatures)

    def parse_webhook(self, body, headers):
        try:
            event = json.loads(body)
        except ValueError:
            raise ConnectorError("The Stripe event was not valid JSON.") from None
        if not isinstance(event, dict) or not str(event.get("id", "")).startswith("evt_"):
            raise ConnectorError("The Stripe event was not understood.")
        kind = event.get("type")
        found = (event.get("data") or {}).get("object") or {}
        metadata = found.get("metadata") if isinstance(found.get("metadata"), dict) else {}
        if kind not in EVENTS or not metadata.get(INVOICE_KEY):
            # Not about a payment through this CRM's links: nothing is kept.
            payload = {"event": "ignored"}
        elif kind == REFUNDED:
            payload = {
                "event": kind,
                "payment": str(found.get("payment_intent") or found.get("id") or ""),
                "amount": found.get("amount"),
                "refunded": bool(found.get("refunded")),
                "currency": str(found.get("currency") or "usd"),
                "invoice": str(metadata[INVOICE_KEY])[:128],
                "account": str(metadata.get(ACCOUNT_KEY) or ""),
                "email": str((found.get("billing_details") or {}).get("email") or "")[:254],
                "created": event.get("created"),
            }
        else:
            payload = {
                "event": kind,
                "payment": str(found.get("payment_intent") or ""),
                "link": str(found.get("payment_link") or ""),
                "paid": found.get("payment_status") == "paid",
                "amount": found.get("amount_total"),
                "currency": str(found.get("currency") or "usd"),
                "invoice": str(metadata[INVOICE_KEY])[:128],
                "account": str(metadata.get(ACCOUNT_KEY) or ""),
                "email": str((found.get("customer_details") or {}).get("email") or "")[:254],
                "created": event.get("created"),
            }
        return WebhookEnvelope(
            event_id=str(event["id"])[:255], capability=Capability.PAYMENTS, payload=payload
        )

    def handle_webhook(self, capability, payload):
        kind = payload["event"]
        if kind == "ignored" or not payload.get("payment"):
            return []
        if kind == COMPLETED and not payload["paid"]:
            # A bank payment: it is recorded when it clears. The link is used meanwhile.
            self.set_link_active(payload["link"], False)
            return []
        if kind == REFUNDED:
            status = "refunded" if payload["refunded"] else "partially_refunded"
            method = "card"
        elif kind == FAILED:
            self.set_link_active(payload["link"], True)
            status, method = "failed", "bank_transfer"
        else:
            self.set_link_active(payload["link"], False)
            status = "completed"
            method = "bank_transfer" if kind == CLEARED else "card"
        account = payload["account"] if payload["account"] in ("operating", "trust") else ""
        created = payload.get("created")
        return [
            PaymentRecord(
                external_id=payload["payment"],
                occurred_at=datetime.fromtimestamp(created, UTC)
                if isinstance(created, int)
                else datetime.now(UTC),
                amount=major_units(payload["amount"], payload["currency"]),
                currency=payload["currency"].upper(),
                status=status,
                reference=payload["invoice"],
                method=method,
                deposit_account=account,
                payer_email=payload["email"],
            )
        ]
