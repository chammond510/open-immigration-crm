from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.comms import system_email
from crm.connect.models import OutboundRequest
from crm.models import Contact, FirmProfile, Matter
from crm.roles import BILLING, MANAGE_BILLING, can, require
from crm.search import words_match

from .forms import (
    BillingSettingsForm,
    InvoiceCreateForm,
    InvoiceLineForm,
    InvoiceNotesForm,
    PaymentForm,
    PaymentPlanForm,
    ReasonForm,
)
from .links import create_links, current_links, link_connections, links_needed, unserved
from .models import BillingSettings, Invoice, InvoiceLine, Payment, StaffRate
from .services import (
    OPEN_STATUSES,
    add_line,
    add_time_to_invoice,
    cancel_plan,
    create_plan,
    delete_draft,
    issue_invoice,
    record,
    record_payment,
    set_email_reminders,
    unbilled_time,
    void_invoice,
    void_payment,
)


def can_bill(user):
    return can(user, BILLING)


billing_required = user_passes_test(can_bill, login_url="login")


def report_errors(request, exc):
    messages.error(request, " ".join(exc.messages))


@billing_required
@require_GET
def invoice_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    invoices = Invoice.objects.select_related("contact", "matter").prefetch_related(
        "lines", "payments"
    )
    if status in Invoice.Status.values:
        invoices = invoices.filter(status=status)
    if query:
        invoices = invoices.filter(
            words_match(
                query,
                ("number", "contact__first_name", "contact__last_name", "matter__matter_number"),
            )
        )
    open_invoices = Invoice.objects.filter(status__in=OPEN_STATUSES).prefetch_related(
        "lines", "payments"
    )
    return render(
        request,
        "billing/invoice_list.html",
        {
            "invoices": invoices[:200],
            "query": query,
            "statuses": Invoice.Status.choices,
            "selected_status": status,
            "outstanding": sum((invoice.balance for invoice in open_invoices), Decimal("0")),
            "overdue_count": sum(1 for invoice in open_invoices if invoice.is_overdue),
            "currency": BillingSettings.load().currency,
        },
    )


@billing_required
@require_http_methods(["GET", "POST"])
def invoice_create(request):
    initial = {}
    for name, model in (("contact", Contact), ("matter", Matter)):
        found = model.objects.filter(pk=request.GET.get(name) or None).first()
        if found:
            initial[name] = found
    if "matter" in initial and "contact" not in initial:
        initial["contact"] = initial["matter"].primary_contact
    form = InvoiceCreateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        invoice = form.save(commit=False)
        invoice.created_by = request.user
        invoice.save()
        record(request.user, "invoice.created", invoice, str(invoice.contact))
        return redirect("invoice_detail", pk=invoice.pk)
    return render(request, "billing/invoice_form.html", {"form": form})


@billing_required
@require_GET
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("contact", "matter"), pk=pk)
    plan = invoice.plan
    can_email = system_email.sender() is not None
    return render(
        request,
        "billing/invoice_detail.html",
        {
            "invoice": invoice,
            "lines": invoice.lines.select_related("time_entry"),
            "payments": invoice.payments.select_related("recorded_by"),
            "unbilled": unbilled_time(invoice) if invoice.is_draft else [],
            "notes_form": InvoiceNotesForm(instance=invoice),
            "line_form": InvoiceLineForm(),
            "payment_form": PaymentForm(initial={"amount": invoice.balance}),
            "earned_total": invoice.subtotal_for(InvoiceLine.Funds.EARNED),
            "advance_total": invoice.subtotal_for(InvoiceLine.Funds.ADVANCE),
            "can_take_payment": invoice.status in OPEN_STATUSES,
            "link_connections": link_connections(),
            "payment_links": current_links(invoice),
            "links_due": links_needed(invoice),
            "plan": plan,
            "schedule": plan.schedule() if plan else [],
            "plan_form": PaymentPlanForm(
                initial={"first_due_date": timezone.localdate() + timedelta(days=30)},
                can_email=can_email,
            ),
            "can_email": can_email,
            "currency": BillingSettings.load().currency,
        },
    )


def draft_or_redirect(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if not invoice.is_draft:
        messages.error(request, "Only draft invoices can be changed.")
        return invoice, redirect("invoice_detail", pk=pk)
    return invoice, None


@billing_required
@require_POST
def invoice_notes(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = InvoiceNotesForm(request.POST, instance=invoice)
    if form.is_valid():
        if not invoice.is_draft and form.cleaned_data["memo"] != Invoice.objects.get(pk=pk).memo:
            messages.error(request, "The client memo is fixed once the invoice is issued.")
        else:
            form.save()
            messages.success(request, "Invoice notes saved.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def invoice_line_add(request, pk):
    invoice, blocked = draft_or_redirect(request, pk)
    if blocked:
        return blocked
    form = InvoiceLineForm(request.POST)
    if form.is_valid():
        add_line(invoice, **form.cleaned_data)
        messages.success(request, "Line added.")
    else:
        messages.error(request, "Review the line fields and try again.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def invoice_line_delete(request, pk, line_pk):
    invoice, blocked = draft_or_redirect(request, pk)
    if blocked:
        return blocked
    get_object_or_404(InvoiceLine, pk=line_pk, invoice=invoice).delete()
    messages.success(request, "Line removed.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def invoice_time_add(request, pk):
    invoice, blocked = draft_or_redirect(request, pk)
    if blocked:
        return blocked
    ids = request.POST.getlist("entries")
    entries = list(unbilled_time(invoice).filter(pk__in=ids))
    if not entries:
        messages.error(request, "Select the time to add.")
        return redirect("invoice_detail", pk=pk)
    try:
        add_time_to_invoice(invoice, entries)
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        noun = "entry" if len(entries) == 1 else "entries"
        messages.success(request, f"Added {len(entries)} time {noun}.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def invoice_issue(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        invoice = issue_invoice(invoice, request.user)
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        messages.success(request, f"Issued {invoice.number}.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        delete_draft(invoice, request.user)
    except ValidationError as exc:
        report_errors(request, exc)
        return redirect("invoice_detail", pk=pk)
    messages.success(request, "Draft deleted.")
    return redirect("invoice_list")


@billing_required
@require_POST
def invoice_void(request, pk):
    require(request.user, MANAGE_BILLING)
    invoice = get_object_or_404(Invoice, pk=pk)
    form = ReasonForm(request.POST)
    try:
        void_invoice(invoice, request.user, form.data.get("reason", ""))
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        messages.success(request, f"{invoice.number} is void; its time can be billed again.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_GET
def invoice_print(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("contact", "matter"), pk=pk)
    if invoice.status == Invoice.Status.VOID:
        messages.error(request, "Void invoices cannot be printed.")
        return redirect("invoice_detail", pk=pk)
    billing = BillingSettings.load()
    return render(
        request,
        "billing/invoice_print.html",
        {
            "invoice": invoice,
            "lines": invoice.lines.all(),
            "payments": invoice.payments.filter(voided_at__isnull=True),
            "schedule": invoice.plan.schedule() if invoice.plan else [],
            "firm": FirmProfile.objects.filter(pk=1).first(),
            "billing": billing,
            "currency": billing.currency,
        },
    )


@billing_required
@require_POST
def payment_record(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = PaymentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Review the payment fields and try again.")
        return redirect("invoice_detail", pk=pk)
    data = dict(form.cleaned_data)
    try:
        record_payment(invoice, request.user, amount=data.pop("amount"), **data)
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        messages.success(request, "Payment recorded.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def payment_links_create(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    connections = {str(c.pk): c for c in link_connections()}
    connection = connections.get(request.POST.get("connection", ""))
    if connection is None:
        raise PermissionDenied
    skipped = unserved(connection, invoice)
    try:
        requests = create_links(connection, invoice, request.user)
    except ValidationError as exc:
        report_errors(request, exc)
        return redirect("invoice_detail", pk=pk)
    for account in skipped:
        messages.warning(
            request,
            f"{connection.label} cannot take payments into the "
            f"{Payment.Account(account).label.lower()}, so no link was made for that part. Use "
            "another payment connection for it, or record the payment by hand.",
        )
    for outbound in requests:
        if outbound.status == OutboundRequest.Status.SENT:
            messages.success(request, f"Created a payment link: {outbound.summary}.")
        elif outbound.status == OutboundRequest.Status.PREVIEWED:
            messages.info(
                request,
                f"{connection.label} is in preview mode, so no link was made. Someone who "
                "manages connections and AI can turn preview mode off on the connection's page.",
            )
            break
        else:
            messages.error(request, f"No payment link was made: {outbound.error}")
    return redirect(reverse("invoice_detail", args=[pk]) + "#payment-links")


@billing_required
@require_POST
def payment_void(request, pk, payment_pk):
    require(request.user, MANAGE_BILLING)
    payment = get_object_or_404(Payment, pk=payment_pk, invoice_id=pk)
    try:
        void_payment(payment, request.user, ReasonForm(request.POST).data.get("reason", ""))
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        messages.success(request, "Payment voided.")
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_http_methods(["GET", "POST"])
def billing_settings(request):
    require(request.user, MANAGE_BILLING)
    billing = BillingSettings.load()
    form = BillingSettingsForm(request.POST or None, instance=billing)
    staff = get_user_model().objects.filter(is_active=True, is_staff=True).order_by("username")
    rates = {rate.user_id: rate.hourly_rate for rate in StaffRate.objects.all()}
    if request.method == "POST" and form.is_valid():
        errors = []
        for person in staff:
            raw = request.POST.get(f"rate_{person.pk}", "").strip()
            if not raw:
                StaffRate.objects.filter(user=person).delete()
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation:
                value = Decimal("-1")
            if value < 0:
                errors.append(str(person))
                continue
            StaffRate.objects.update_or_create(user=person, defaults={"hourly_rate": value})
        if errors:
            messages.error(request, f"Enter a valid rate for {', '.join(errors)}.")
        else:
            form.save()
            record(request.user, "billing.settings_changed", billing)
            messages.success(request, "Billing settings saved.")
            return redirect("billing_settings")
    return render(
        request,
        "billing/settings.html",
        {"form": form, "staff": [(person, rates.get(person.pk)) for person in staff]},
    )


@billing_required
@require_POST
def plan_create(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = PaymentPlanForm(request.POST, can_email=system_email.sender() is not None)
    if not form.is_valid():
        messages.error(request, "Review the payment plan fields and try again.")
        return redirect("invoice_detail", pk=pk)
    try:
        plan = create_plan(invoice, request.user, **form.cleaned_data)
    except ValidationError as exc:
        report_errors(request, exc)
    else:
        count = plan.installments.count()
        reminders = "staff and email reminders" if plan.email_reminders else "staff reminders"
        messages.success(
            request, f"Payment plan created with {count} installments and {reminders}."
        )
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def plan_email_reminders(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    plan = invoice.plan
    on = request.POST.get("on") == "1"
    if plan is None:
        messages.error(request, "This invoice has no payment plan.")
    elif on and system_email.sender() is None:
        messages.error(request, "Connect an email delivery service first.")
    else:
        try:
            set_email_reminders(plan, request.user, on)
        except ValidationError as exc:
            report_errors(request, exc)
        else:
            messages.success(
                request, "Email reminders turned on." if on else "Email reminders turned off."
            )
    return redirect("invoice_detail", pk=pk)


@billing_required
@require_POST
def plan_cancel(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    plan = invoice.plan
    if plan is None:
        messages.error(request, "This invoice has no payment plan.")
        return redirect("invoice_detail", pk=pk)
    cancel_plan(plan, request.user)
    messages.success(request, "Payment plan canceled; its open reminders were removed.")
    return redirect("invoice_detail", pk=pk)
