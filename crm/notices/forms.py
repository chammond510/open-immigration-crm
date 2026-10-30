from django import forms
from django.utils import timezone

from crm.models import Contact

from .models import CaseNotice, normalize_receipt_number, validate_receipt_number

DATE = forms.DateInput(attrs={"type": "date"})


class CaseNoticeForm(forms.ModelForm):
    # Wider than the stored 13 characters so pasted numbers with dashes or spaces
    # reach clean_receipt_number, which normalizes them before validation.
    receipt_number = forms.CharField(max_length=20, required=False)

    class Meta:
        model = CaseNotice
        fields = [
            "kind",
            "form_type",
            "receipt_number",
            "notice_date",
            "uscis_received_date",
            "priority_date",
            "response_due_date",
            "appointment_at",
            "appointment_location",
            "contact",
            "document",
            "notes",
        ]
        widgets = {
            "notice_date": DATE,
            "uscis_received_date": DATE,
            "priority_date": DATE,
            "response_due_date": DATE,
            "appointment_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, matter, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.matter = matter
        party_ids = matter.parties.values_list("contact_id", flat=True)
        self.fields["contact"].queryset = Contact.objects.filter(
            pk__in=[matter.primary_contact_id, *party_ids]
        )
        self.fields["contact"].empty_label = f"{matter.primary_contact} (primary contact)"
        self.fields["document"].queryset = matter.documents.all()
        self.fields["document"].empty_label = "No scanned copy linked"

    def clean_receipt_number(self):
        value = normalize_receipt_number(self.cleaned_data.get("receipt_number"))
        validate_receipt_number(value)
        return value


class StatusCheckForm(forms.Form):
    receipt_number = forms.CharField(max_length=20)
    status_title = forms.CharField(label="Status", max_length=255)
    status_text = forms.CharField(
        label="Details", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    checked_at = forms.DateTimeField(
        label="Checked",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        initial=lambda: timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
    )

    def clean_receipt_number(self):
        value = normalize_receipt_number(self.cleaned_data["receipt_number"])
        validate_receipt_number(value)
        return value
