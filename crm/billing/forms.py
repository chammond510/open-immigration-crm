from django import forms

from crm.models import Contact, Matter

from .models import BillingSettings, Invoice, InvoiceLine, Payment, PaymentPlan


class InvoiceCreateForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["contact", "matter", "memo"]
        labels = {"contact": "Bill to", "matter": "Matter (optional)"}
        widgets = {"memo": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].required = False
        self.fields["contact"].queryset = Contact.objects.order_by("last_name", "first_name")
        self.fields["matter"].queryset = Matter.objects.select_related("primary_contact")

    def clean(self):
        cleaned = super().clean()
        matter = cleaned.get("matter")
        if matter and not cleaned.get("contact"):
            cleaned["contact"] = matter.primary_contact
        if not cleaned.get("contact"):
            raise forms.ValidationError("Choose who the invoice is for.")
        return cleaned

    def save(self, commit=True):
        self.instance.contact = self.cleaned_data["contact"]
        return super().save(commit=commit)


class InvoiceNotesForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["memo", "notes"]
        widgets = {
            "memo": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class InvoiceLineForm(forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["kind", "funds", "description", "quantity", "unit_price"]
        labels = {"kind": "Type", "funds": "Funds", "unit_price": "Unit price"}


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["received_on", "amount", "method", "deposit_account", "reference", "note"]
        labels = {"received_on": "Received", "deposit_account": "Deposited to"}
        widgets = {
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"inputmode": "decimal"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = [("", "Choose a method"), *Payment.Method.choices]
        self.fields["deposit_account"].choices = [
            ("", "Choose an account"),
            *Payment.Account.choices,
        ]


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=255)


class BillingSettingsForm(forms.ModelForm):
    class Meta:
        model = BillingSettings
        fields = [
            "currency",
            "invoice_prefix",
            "default_hourly_rate",
            "payment_terms_days",
            "time_increment_minutes",
            "payment_instructions",
        ]
        labels = {"default_hourly_rate": "Firm default hourly rate"}
        widgets = {"payment_instructions": forms.Textarea(attrs={"rows": 3})}


class PaymentPlanForm(forms.Form):
    count = forms.IntegerField(label="Number of installments", min_value=2, max_value=60)
    frequency = forms.ChoiceField(label="How often", choices=PaymentPlan.Frequency.choices)
    first_due_date = forms.DateField(
        label="First installment due", widget=forms.DateInput(attrs={"type": "date"})
    )
    reminder_days_before = forms.IntegerField(
        label="Remind staff this many days before each due date",
        min_value=0,
        max_value=30,
        initial=3,
    )

    def __init__(self, *args, can_email=False, **kwargs):
        super().__init__(*args, **kwargs)
        if can_email:
            self.fields["email_reminders"] = forms.BooleanField(
                label="Also email the client a reminder then",
                required=False,
                help_text="Sent at 9 a.m. through the firm's email delivery service, to the "
                "invoice contact's email address.",
            )
