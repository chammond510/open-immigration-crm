"""Payment links: a processor's page where a client pays an invoice online.

Core knows only the ``create_payment_link`` action that payment connectors
declare, with the payload ``{"invoice_number", "amount" (a decimal string),
"currency", "deposit_account" ("operating" or "trust"), "email", "name"}``; the
connector returns the link's HTTPS address. Each link goes through the outbound
policy, which holds it in preview mode and records the attempt.

Advances belong in trust and earned fees in operating, so an invoice with both
gets one link per account, each for what is still unpaid of that part. The
payment comes back through the connector with the invoice number and the account,
and is recorded on the invoice like any connector payment.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import Connection, OutboundRequest

from .models import BillingSettings, InvoiceLine, Payment, PaymentLink, money
from .services import OPEN_STATUSES

CREATE_PAYMENT_LINK = "create_payment_link"
FUNDS = {
    Payment.Account.TRUST: InvoiceLine.Funds.ADVANCE,
    Payment.Account.OPERATING: InvoiceLine.Funds.EARNED,
}


def link_connections():
    """Active firm-wide connections that can make payment links."""
    return [
        connection
        for connection in Connection.objects.filter(
            status=Connection.Status.ACTIVE, owner__isnull=True
        )
        if connection.connector_class is not None
        and Capability.PAYMENTS in connection.enabled_capabilities
        and connection.connector_class.outbound_actions.get(CREATE_PAYMENT_LINK)
        == Capability.PAYMENTS
    ]


def amounts_due(invoice):
    """What is still unpaid into each account: advances into trust, earned fees into operating."""
    if invoice.status not in OPEN_STATUSES:
        return {}
    due = {}
    for account, funds in FUNDS.items():
        paid = invoice.payments.filter(voided_at__isnull=True, deposit_account=account).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0")
        amount = min(money(invoice.subtotal_for(funds) - paid), invoice.balance)
        if amount > 0:
            due[account] = amount
    return due


def links_needed(invoice):
    """Amounts owed into each account that no existing link already asks for."""
    return {
        account: amount
        for account, amount in amounts_due(invoice).items()
        if not invoice.payment_links.filter(deposit_account=account, amount=amount).exists()
    }


def current_links(invoice):
    """The invoice's links, each marked whether it still asks for what is owed."""
    due = amounts_due(invoice)
    links = list(invoice.payment_links.select_related("source_connection"))
    for link in links:
        link.is_current = due.get(link.deposit_account) == link.amount
    return links


def create_links(connection, invoice, actor):
    """Ask the processor for a link per account still owed; return the policy's requests."""
    if not amounts_due(invoice):
        raise ValidationError("Nothing is left to pay on this invoice.")
    due = links_needed(invoice)
    if not due:
        raise ValidationError("Links for what is owed already exist; copy them below.")
    contact = invoice.contact
    requests = []
    for account, amount in due.items():
        request = policy.send(
            connection,
            CREATE_PAYMENT_LINK,
            {
                "invoice_number": invoice.number,
                "amount": str(amount),
                "currency": BillingSettings.load().currency,
                "deposit_account": account,
                "email": contact.email,
                "name": contact.full_name,
            },
            actor=actor,
            target=invoice,
            summary=f"Payment link for {invoice.number} ({account})",
        )
        url = getattr(request, "result", "")
        if request.status == OutboundRequest.Status.SENT and not url.startswith("https://"):
            request.status = OutboundRequest.Status.FAILED
            request.error = "The processor did not return an HTTPS link."
            request.save(update_fields=["status", "error"])
        elif request.status == OutboundRequest.Status.SENT:
            PaymentLink.objects.create(
                invoice=invoice,
                deposit_account=account,
                amount=amount,
                url=url,
                source_connection=connection,
                created_by=actor,
            )
        requests.append(request)
    return requests
