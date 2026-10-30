"""LawPay (8am, formerly AffiniPay): payment links and payments for invoices.

The firm connects with its LawPay secret key; the API uses it for HTTP Basic
authentication. Payment links are the firm's LawPay hosted payment pages,
prefilled with the amount, the invoice number as the reference, and the client's
name and email, with the amount and reference read-only. Each invoice gets one
link per account, so advances are paid into the trust account's page and earned
fees into the operating account's page.

Payments come back by polling LawPay's event list every few minutes, which its
documentation recommends. LawPay's webhooks are not signed, so a webhook is
taken only as a hint: the CRM fetches the event it names from LawPay itself and
never trusts the delivery's contents. Each charge's account says whether it went
to trust or operating, from the accounts chosen on the connection or LawPay's own
trust flag; a charge whose account is neither is left for staff.

API reference: https://developers.8am.com/reference/api
"""

import json
import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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
from crm.connect.records import PaymentRecord
from crm.connect.registry import register

API_URL = "https://api.8am.com/v1"
PAGE_HOSTS = ("lawpay.com", "affinipay.com", "8am.com")
EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PAGE_SIZE = 100
EVENT_LIMIT = 2000
# Event times overlap by this much between polls; payments are recorded once by ID.
POLL_OVERLAP = timedelta(minutes=5)
STATUSES = {"COMPLETED": "completed", "FAILED": "failed", "VOIDED": "refunded"}
METHODS = {"card": "card", "bank": "bank_transfer"}


def validate_page(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme != "https" or not any(host == h or host.endswith(f".{h}") for h in PAGE_HOSTS):
        raise ValidationError(
            "Enter the payment page address LawPay shows under Show & Copy Page Link, "
            "starting with https://."
        )


def account_options(connector):
    return connector.accounts()


@register
class LawPayConnector(Connector):
    key = "lawpay"
    name = "LawPay"
    description = (
        "Send clients payment links for an invoice, with advances paid into trust and earned "
        "fees into operating, and record LawPay payments and refunds on the invoice."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.PAYMENTS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "secret_key",
                "Secret key",
                help_text="In LawPay, Settings > Integrations > Add New Application > My "
                "Website, or Developers > Show Credentials. Use the live key.",
            ),
        ),
        docs_url="https://developers.8am.com/connect/connect-merchant",
        label="Secret key",
    )
    settings_fields = (
        SettingField(
            "operating_page",
            "Operating account's payment page",
            required=False,
            help_text="From LawPay's New Charge page: Actions > Show & Copy Page Link, with the "
            "operating account chosen.",
            validate=validate_page,
        ),
        SettingField(
            "trust_page",
            "Trust account's payment page",
            required=False,
            help_text="The same, with the trust (IOLTA) account chosen.",
            validate=validate_page,
        ),
        SettingField(
            "trust_accounts",
            "LawPay accounts that are trust accounts",
            options=account_options,
            multiple=True,
            help_text="Payments into these are recorded to trust. Unticked accounts LawPay "
            "marks as trust count too.",
        ),
        SettingField(
            "operating_accounts",
            "LawPay accounts that are operating accounts",
            options=account_options,
            multiple=True,
            help_text="Payments into these are recorded to operating. A payment into any "
            "other account waits for staff.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Payment links: the invoice number, the amount, and the client's name and email, "
            "in the address of the firm's LawPay payment page",
        ),
        receives=(
            "Payments and refunds made through LawPay: the amount, the reference (the invoice "
            "number), the account paid into, the payment method, and the payer's email",
            "The names of the firm's LawPay accounts, to map them to operating and trust",
        ),
    )
    outbound_actions = {"create_payment_link": Capability.PAYMENTS}
    sync_capabilities = frozenset({Capability.PAYMENTS})
    sync_interval = timedelta(minutes=10)
    webhooks = WebhookSpec(
        capabilities=(Capability.PAYMENTS,),
        instructions="Optional; payments are also checked every 10 minutes. In LawPay, open "
        "Settings > Integrations > Add New Webhook URL and paste this address. LawPay does not "
        "sign webhooks, so the CRM fetches each event from LawPay itself and ignores the "
        "delivery's contents. The signing secret below is not used.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="LawPay",
            headers={"Accept": "application/json"},
            auth=(self.credentials.get("secret_key", ""), ""),
        )
        self.account_list = None

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError("LawPay did not accept the secret key.", status=401) from None
            raise

    def merchant_accounts(self):
        """Every card and eCheck account, with LawPay's own trust flag where it gives one."""
        if self.account_list is None:
            merchant = self.call("GET", "/merchant")
            self.account_list = [
                {**account, "kind": kind}
                for key, kind in (("merchant_accounts", "card"), ("ach_accounts", "eCheck"))
                for account in merchant.get(key) or []
                if account.get("id") and account.get("status") != "DELETED"
            ]
        return self.account_list

    def accounts(self):
        """(id, name) choices for the account settings."""
        choices = []
        for account in self.merchant_accounts():
            flag = " · trust at LawPay" if account.get("trust_account") is True else ""
            label = f"{account.get('name') or account['id']} ({account['kind']}{flag})"
            choices.append((account["id"], label))
        return choices

    def deposit_account(self, account_id):
        if account_id in self.setting("trust_accounts"):
            return "trust"
        if account_id in self.setting("operating_accounts"):
            return "operating"
        account = next((a for a in self.merchant_accounts() if a["id"] == account_id), {})
        flag = account.get("trust_account")
        if flag in (True, "true"):
            return "trust"
        if flag in (False, "false"):
            return "operating"
        return ""

    def test_connection(self):
        self.merchant_accounts()

    def perform(self, action, payload):
        if action != "create_payment_link":
            raise ConnectorError(f"LawPay cannot {action}.")
        page = self.setting(f"{payload['deposit_account']}_page")
        if not page:
            raise ConnectorError(
                f"Enter the {payload['deposit_account']} account's LawPay payment page on the "
                "connection's page first."
            )
        params = {
            "amount": payload["amount"],
            "reference": payload["invoice_number"],
            "name": payload.get("name", ""),
            "email": payload.get("email", ""),
            "readOnlyFields": "amount,reference",
        }
        separator = "&" if "?" in page else "?"
        return f"{page}{separator}{urlencode({k: v for k, v in params.items() if v})}"

    # Payments

    def sync(self, capability, cursor):
        now = timezone.now()
        since = parse_datetime(str((cursor or {}).get("since") or ""))
        if since is None:
            # The first poll starts now: earlier payments may already be recorded by hand.
            return SyncBatch(records=[], cursor={"since": now.isoformat()})
        start = (since - POLL_OVERLAP).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        records, latest, page = [], since, 1
        while True:
            answer = self.call(
                "GET", "/events", params={"start_date": start, "page": page, "page_size": PAGE_SIZE}
            )
            results = answer.get("results") or []
            for event in results:
                records += self.records_for(event)
                created = parse_datetime(str(event.get("created") or ""))
                latest = max(latest, created) if created else latest
            total = int(answer.get("total_entries") or 0)
            if page * PAGE_SIZE >= total or not results or page * PAGE_SIZE >= EVENT_LIMIT:
                break
            page += 1
        return SyncBatch(records=records, cursor={"since": latest.isoformat()})

    def records_for(self, event):
        """The payment record an event about a transaction means, if any."""
        if not str(event.get("type", "")).startswith("transaction."):
            return []
        data = event.get("data") or {}
        if data.get("type") == "REFUND":
            return self.refund_record(data)
        if data.get("type") != "CHARGE" or data.get("status") not in STATUSES:
            return []
        return [self.charge_record(data, STATUSES[data["status"]])]

    def charge_record(self, charge, status):
        method = charge.get("method") or {}
        occurred = parse_datetime(
            str(charge.get("completion_timestamp") or charge.get("created") or "")
        )
        return PaymentRecord(
            external_id=str(charge["id"]),
            occurred_at=occurred or timezone.now(),
            amount=str((Decimal(int(charge.get("amount") or 0)) / 100).quantize(Decimal("0.01"))),
            currency=str(charge.get("currency") or "USD"),
            status=status,
            reference=str(charge.get("reference") or "")[:128],
            method=METHODS.get(str(method.get("type") or "").lower(), "other"),
            deposit_account=self.deposit_account(charge.get("account_id")),
            payer_email=str(method.get("email") or "")[:254],
        )

    def refund_record(self, refund):
        """A completed refund voids its charge when it returns the whole amount."""
        if refund.get("status") != "COMPLETED" or not refund.get("charge_id"):
            return []
        charge = self.call("GET", f"/transactions/{refund['charge_id']}")
        if charge.get("type") != "CHARGE" or charge.get("status") != "COMPLETED":
            return []
        whole = int(charge.get("amount_refunded") or 0) >= int(charge.get("amount") or 0)
        return [self.charge_record(charge, "refunded" if whole else "partially_refunded")]

    # Webhooks, taken only as hints

    def verify_webhook(self, body, headers, secret, *, url=""):
        # LawPay does not sign webhooks. The private address keeps strangers out, and the
        # event is fetched from LawPay itself, so a forged delivery can only prompt a check.
        try:
            event = json.loads(body)
        except ValueError:
            return False
        return isinstance(event, dict) and bool(EVENT_ID.match(str(event.get("id") or "")))

    def parse_webhook(self, body, headers):
        event_id = str(json.loads(body)["id"])
        return WebhookEnvelope(
            event_id=event_id, capability=Capability.PAYMENTS, payload={"event_id": event_id}
        )

    def handle_webhook(self, capability, payload):
        """Fetch the event the webhook named from LawPay; the delivery itself is not used."""
        try:
            event = self.call("GET", f"/events/{payload['event_id']}")
        except ConnectorError as exc:
            if exc.status == 404:
                return []
            raise
        return self.records_for(event)
