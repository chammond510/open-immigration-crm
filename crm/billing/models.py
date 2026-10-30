import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from crm.models import Contact, Matter, TimeEntry

CENT = Decimal("0.01")


def money(value):
    return (value or Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(value, currency="USD"):
    amount = money(Decimal(value or 0))
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.2f}"
    return f"{sign}${text}" if currency == "USD" else f"{sign}{text} {currency}"


class BillingSettings(models.Model):
    """Firm-wide billing defaults; a single row."""

    currency = models.CharField(max_length=3, default="USD")
    invoice_prefix = models.CharField(max_length=12, default="INV")
    invoice_sequence_year = models.PositiveIntegerField(default=0)
    invoice_sequence_value = models.PositiveIntegerField(default=0)
    default_hourly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    time_increment_minutes = models.PositiveSmallIntegerField(
        default=6,
        validators=[MinValueValidator(1)],
        help_text="Billed time rounds up to this increment (6 minutes is a tenth of an hour).",
    )
    payment_instructions = models.TextField(
        blank=True, help_text="Printed on every invoice, such as how to pay by check."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "billing settings"
        verbose_name_plural = "billing settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        self.invoice_prefix = (self.invoice_prefix or "INV").strip().upper()
        self.currency = (self.currency or "USD").strip().upper()
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def next_invoice_number(cls):
        with transaction.atomic():
            cls.objects.get_or_create(pk=1)
            billing = cls.objects.select_for_update().get(pk=1)
            year = timezone.localdate().year
            if billing.invoice_sequence_year != year:
                billing.invoice_sequence_year = year
                billing.invoice_sequence_value = 0
            billing.invoice_sequence_value += 1
            billing.save(update_fields=["invoice_sequence_year", "invoice_sequence_value"])
            return f"{billing.invoice_prefix}-{year}-{billing.invoice_sequence_value:04d}"

    def __str__(self):
        return "Billing settings"


class StaffRate(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="billing_rate"
    )
    hourly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )

    def __str__(self):
        return f"{self.user}: {self.hourly_rate}"


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Assigned when the invoice is issued, so drafts never leave gaps in numbering.
    number = models.CharField(max_length=30, unique=True, null=True, blank=True)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="invoices")
    matter = models.ForeignKey(
        Matter, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    memo = models.TextField(blank=True, help_text="Printed on the invoice for the client.")
    notes = models.TextField(blank=True, help_text="Internal; never printed.")
    void_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("access_billing", "Can view and manage billing")]

    def __str__(self):
        return self.number or f"Draft invoice for {self.contact}"

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def subtotal(self):
        return money(sum((line.amount for line in self.lines.all()), Decimal("0")))

    def subtotal_for(self, funds):
        return money(
            sum((line.amount for line in self.lines.all() if line.funds == funds), Decimal("0"))
        )

    @property
    def amount_paid(self):
        total = self.payments.filter(voided_at__isnull=True).aggregate(total=Sum("amount"))
        return money(total["total"])

    @property
    def balance(self):
        if self.status == self.Status.VOID:
            return Decimal("0.00")
        return money(self.subtotal - self.amount_paid)

    @property
    def plan(self):
        """The payment plan in force (active or completed), if any."""
        return self.payment_plans.exclude(status="canceled").first()

    @property
    def is_overdue(self):
        return (
            self.status in (self.Status.ISSUED, self.Status.PARTIALLY_PAID)
            and self.due_date is not None
            and self.due_date < timezone.localdate()
        )


class InvoiceLine(models.Model):
    class Kind(models.TextChoices):
        LEGAL_FEE = "legal_fee", "Legal fee"
        CONSULTATION = "consultation", "Consultation"
        FILING_FEE = "filing_fee", "Government filing fee"
        TRANSLATION = "translation", "Translation"
        COURT_FEE = "court_fee", "Court or agency cost"
        OTHER = "other", "Other"

    class Funds(models.TextChoices):
        EARNED = "earned", "Earned fee (operating)"
        ADVANCE = "advance", "Advance (trust)"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.LEGAL_FEE)
    funds = models.CharField(max_length=10, choices=Funds.choices, default=Funds.EARNED)
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(
        max_digits=9, decimal_places=2, default=1, validators=[MinValueValidator(0)]
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    # Each time entry can be billed once; deleting the entry keeps the line.
    time_entry = models.OneToOneField(
        TimeEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_line",
    )

    class Meta:
        ordering = ["position", "pk"]

    @property
    def amount(self):
        return money(Decimal(self.quantity) * Decimal(self.unit_price))

    def __str__(self):
        return self.description


class Payment(models.Model):
    class Method(models.TextChoices):
        CHECK = "check", "Check"
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CASH = "cash", "Cash"
        MONEY_ORDER = "money_order", "Money order"
        OTHER = "other", "Other"

    class Account(models.TextChoices):
        OPERATING = "operating", "Operating account"
        TRUST = "trust", "Trust account"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    received_on = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    deposit_account = models.CharField(max_length=20, choices=Account.choices)
    reference = models.CharField(
        max_length=120, blank=True, help_text="Check number or confirmation number."
    )
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Set by payment connectors; empty for payments recorded by hand.
    source_connection = models.ForeignKey(
        "connect.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    external_id = models.CharField(max_length=255, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_connection", "external_id"],
                condition=~models.Q(external_id=""),
                name="billing_one_payment_per_provider_id",
            ),
        ]

    def clean(self):
        if self.invoice_id and self.invoice.status in (
            Invoice.Status.DRAFT,
            Invoice.Status.VOID,
        ):
            raise ValidationError("Payments can be recorded only on issued invoices.")

    def __str__(self):
        return f"{self.amount} on {self.received_on}"


class PaymentPlan(models.Model):
    """An issued invoice's balance split into scheduled installments.

    Payments are applied to installments oldest first, so an installment's
    status follows from how much of the invoice has been paid.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        CANCELED = "canceled", "Canceled"

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        BIWEEKLY = "biweekly", "Every two weeks"
        WEEKLY = "weekly", "Weekly"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payment_plans")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    reminder_days_before = models.PositiveSmallIntegerField(default=3)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Canceled plans stay for the record; only one plan at a time is in force.
            models.UniqueConstraint(
                fields=["invoice"],
                condition=~models.Q(status="canceled"),
                name="billing_one_plan_in_force",
            ),
        ]

    def __str__(self):
        return f"Payment plan for {self.invoice}"

    def schedule(self):
        """Installments with the amount applied to each and a display status."""
        today = timezone.localdate()
        remaining = self.invoice.amount_paid
        rows = []
        for installment in self.installments.all():
            applied = min(remaining, installment.amount)
            remaining -= applied
            if applied >= installment.amount:
                state = "paid"
            elif installment.due_date < today:
                state = "overdue"
            elif applied > 0:
                state = "partially_paid"
            else:
                state = "due"
            rows.append({"installment": installment, "applied": money(applied), "state": state})
        return rows

    def next_due(self):
        return next((row for row in self.schedule() if row["state"] != "paid"), None)


class PaymentPlanInstallment(models.Model):
    plan = models.ForeignKey(PaymentPlan, on_delete=models.CASCADE, related_name="installments")
    sequence = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    # The staff reminder created for this installment; completed when it is paid.
    reminder = models.OneToOneField(
        "crm.WorkItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment_plan_installment",
    )

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "sequence"], name="billing_installment_once"),
        ]

    def __str__(self):
        return f"Installment {self.sequence} of {self.plan}"


class PaymentLink(models.Model):
    """A link a payment processor made for paying part of an invoice into one account.

    An invoice with both earned fees and advances gets one link per account, so
    advances are paid into trust and earned fees into operating, never mixed.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payment_links")
    deposit_account = models.CharField(max_length=20, choices=Payment.Account.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    url = models.URLField(max_length=2000)
    source_connection = models.ForeignKey(
        "connect.Connection", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_deposit_account_display()} link for {self.invoice}"
