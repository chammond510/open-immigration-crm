from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Activity,
    ChecklistItem,
    Contact,
    Document,
    FirmProfile,
    IntakeForm,
    IntakeInvite,
    Matter,
    MatterParty,
    WorkItem,
)
from .validators import validate_document

DATE_INPUT = forms.DateInput(attrs={"type": "date"})
DATETIME_INPUT = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "date_of_birth",
            "preferred_language",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "country_of_birth",
            "nationality",
            "a_number",
            "current_immigration_status",
            "status",
            "stage",
            "assigned_to",
            "notes",
        ]
        widgets = {
            "date_of_birth": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
        )


class MatterForm(forms.ModelForm):
    class Meta:
        model = Matter
        fields = [
            "primary_contact",
            "title",
            "case_type",
            "status",
            "assigned_to",
            "receipt_number",
            "priority_date",
            "filing_date",
            "description",
            "notes",
        ]
        widgets = {
            "priority_date": DATE_INPUT,
            "filing_date": DATE_INPUT,
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["primary_contact"].queryset = Contact.objects.all()
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
        )


class MatterPartyForm(forms.ModelForm):
    class Meta:
        model = MatterParty
        fields = ["contact", "role", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class ActivityForm(forms.ModelForm):
    class Meta:
        model = Activity
        fields = ["kind", "subject", "body", "occurred_at"]
        widgets = {
            "kind": forms.Select(attrs={"aria-label": "Activity type"}),
            "subject": forms.TextInput(
                attrs={"aria-label": "Activity subject", "placeholder": "Subject"}
            ),
            "body": forms.Textarea(
                attrs={"rows": 3, "aria-label": "Activity details", "placeholder": "Details"}
            ),
            "occurred_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "aria-label": "Activity date and time"},
                format="%Y-%m-%dT%H:%M",
            ),
        }


class WorkItemForm(forms.ModelForm):
    class Meta:
        model = WorkItem
        fields = [
            "kind",
            "title",
            "description",
            "priority",
            "contact",
            "matter",
            "assigned_to",
            "scheduled_for",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "scheduled_for": DATETIME_INPUT,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
        )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("contact") and not cleaned.get("matter"):
            raise forms.ValidationError("Link the item to a contact or matter.")
        return cleaned


class ChecklistItemForm(forms.ModelForm):
    class Meta:
        model = ChecklistItem
        fields = ["title", "description", "position"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class DocumentUploadForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["title", "category", "contact", "file"]

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        self.detected_mime = validate_document(uploaded)
        return uploaded


class IntakeFormConfigForm(forms.ModelForm):
    questions_text = forms.CharField(
        required=False,
        label="Questions",
        help_text="One question per line. No questions are provided by the project.",
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "Enter one question per line"}),
    )

    class Meta:
        model = IntakeForm
        fields = ["name", "instructions", "questions_text", "is_active"]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["questions_text"].initial = "\n".join(
                question.get("label", "") for question in self.instance.questions
            )

    def clean_questions_text(self):
        lines = [line.strip() for line in self.cleaned_data["questions_text"].splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > 40:
            raise forms.ValidationError("An intake form may contain at most 40 questions.")
        return lines

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.questions = [
            {"key": f"q{index + 1}-{slugify(label)[:40]}", "label": label}
            for index, label in enumerate(self.cleaned_data["questions_text"])
        ]
        if commit:
            instance.save()
        return instance


class IntakeInviteForm(forms.Form):
    intake_form = forms.ModelChoiceField(queryset=IntakeForm.objects.none())
    contact = forms.ModelChoiceField(queryset=Contact.objects.none())
    matter = forms.ModelChoiceField(queryset=Matter.objects.none(), required=False)
    expires_in_days = forms.IntegerField(min_value=1, max_value=30, initial=7)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["intake_form"].queryset = IntakeForm.objects.filter(is_active=True)
        self.fields["contact"].queryset = Contact.objects.all()
        self.fields["matter"].queryset = Matter.objects.exclude(status=Matter.Status.CLOSED)

    def clean(self):
        cleaned = super().clean()
        matter = cleaned.get("matter")
        contact = cleaned.get("contact")
        if matter and contact and matter.primary_contact_id != contact.id:
            is_party = matter.parties.filter(contact=contact).exists()
            if not is_party:
                raise forms.ValidationError("The selected contact is not linked to that matter.")
        return cleaned

    def issue(self, user):
        days = self.cleaned_data["expires_in_days"]
        return IntakeInvite.issue(
            intake_form=self.cleaned_data["intake_form"],
            contact=self.cleaned_data["contact"],
            matter=self.cleaned_data.get("matter"),
            expires_at=timezone.now() + timedelta(days=days),
            created_by=user,
        )


class PublicIntakeForm(forms.Form):
    email = forms.EmailField(required=False, help_text="Optional updated email address")
    phone = forms.CharField(
        required=False, max_length=40, help_text="Optional updated phone number"
    )

    def __init__(self, *args, intake_form, **kwargs):
        super().__init__(*args, **kwargs)
        self.intake_form = intake_form
        for question in intake_form.questions:
            self.fields[question["key"]] = forms.CharField(
                label=question["label"],
                required=False,
                widget=forms.Textarea(attrs={"rows": 3}),
            )

    @property
    def answers(self):
        return {
            question["key"]: {
                "label": question["label"],
                "answer": self.cleaned_data.get(question["key"], ""),
            }
            for question in self.intake_form.questions
        }


class FirmProfileForm(forms.ModelForm):
    class Meta:
        model = FirmProfile
        fields = [
            "name",
            "short_name",
            "email",
            "phone",
            "website",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "matter_prefix",
        ]
