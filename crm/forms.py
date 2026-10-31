from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone
from django.utils.text import slugify

from .comms.system_email import address_problem
from .customfields.forms import CustomFieldsMixin
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


def staff_choices(field):
    """Limit a user field to active staff, shown by name rather than username."""
    field.queryset = get_user_model().objects.filter(is_active=True, is_staff=True)
    field.label_from_instance = lambda user: user.get_full_name() or user.username
    field.empty_label = "Unassigned"


class SectionedFormMixin:
    """Groups a form's fields under headings on the form page. Fields not named in
    `sections`, such as custom fields, come last under "More details"."""

    sections = ()

    def field_sections(self):
        named = {name for _, names in self.sections for name in names}
        groups = [
            (title, [self[name] for name in names if name in self.fields])
            for title, names in self.sections
        ]
        rest = [self[name] for name in self.fields if name not in named]
        if rest:
            groups.append(("More details", rest))
        return [(title, fields) for title, fields in groups if fields]


class FirstRunSetupForm(UserCreationForm):
    """Creates the first administrator while the installation has no accounts."""

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")


class TwoFactorCodeForm(forms.Form):
    code = forms.CharField(
        label="Authentication code",
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.prefix:
            # A second code form on a settings page should not take focus on load.
            self.fields["code"].widget.attrs.pop("autofocus", None)


class TwoFactorDisableForm(forms.Form):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    code = forms.CharField(
        label="Authentication code",
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code"}),
    )


class ContactForm(SectionedFormMixin, CustomFieldsMixin, forms.ModelForm):
    custom_fields_for = "contact"
    sections = (
        ("Name and contact", ("first_name", "last_name", "email", "phone", "preferred_language")),
        (
            "Immigration details",
            (
                "date_of_birth",
                "country_of_birth",
                "nationality",
                "a_number",
                "current_immigration_status",
            ),
        ),
        (
            "Address",
            ("address_line1", "address_line2", "city", "region", "postal_code", "country"),
        ),
        ("Relationship", ("status", "stage", "assigned_to", "notes")),
    )

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
            "email": forms.EmailInput(attrs={"autocomplete": "off"}),
            "phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"}),
            "date_of_birth": DATE_INPUT,
            "a_number": forms.TextInput(
                attrs={
                    "placeholder": "A123456789",
                    "autocapitalize": "characters",
                    "spellcheck": "false",
                }
            ),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "region": "State or region",
            "current_immigration_status": "Current immigration status",
            "assigned_to": "Owner",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        staff_choices(self.fields["assigned_to"])
        self.add_custom_fields()


class MatterForm(SectionedFormMixin, CustomFieldsMixin, forms.ModelForm):
    custom_fields_for = "matter"
    sections = (
        ("Matter", ("primary_contact", "title", "case_type", "status", "assigned_to")),
        ("USCIS", ("receipt_number", "priority_date", "filing_date")),
        ("Notes", ("description", "notes")),
    )

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
            "receipt_number": forms.TextInput(
                attrs={
                    "placeholder": "IOE0123456789",
                    "autocapitalize": "characters",
                    "spellcheck": "false",
                }
            ),
            "priority_date": DATE_INPUT,
            "filing_date": DATE_INPUT,
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {"assigned_to": "Owner"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["primary_contact"].queryset = Contact.objects.all()
        self.fields["primary_contact"].empty_label = "Choose a contact"
        staff_choices(self.fields["assigned_to"])
        self.add_custom_fields()


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
            "title",
            "kind",
            "priority",
            "scheduled_for",
            "assigned_to",
            "contact",
            "matter",
            "description",
            "meeting_url",
        ]
        labels = {"kind": "Type", "scheduled_for": "Due or scheduled for"}
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "scheduled_for": DATETIME_INPUT,
        }
        help_texts = {
            "meeting_url": "For an appointment. Left blank, a video meeting is made when the "
            "assigned staff member has connected one.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        staff_choices(self.fields["assigned_to"])
        self.fields["contact"].empty_label = "No contact"
        self.fields["matter"].empty_label = "No matter"

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
        if any(len(line) > 300 for line in lines):
            raise forms.ValidationError("Keep each question under 300 characters.")
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
    intake_form = forms.ModelChoiceField(
        queryset=IntakeForm.objects.none(), empty_label="Choose a form"
    )
    contact = forms.ModelChoiceField(
        queryset=Contact.objects.none(), empty_label="Choose a contact"
    )
    matter = forms.ModelChoiceField(
        queryset=Matter.objects.none(), required=False, empty_label="No matter"
    )
    expires_in_days = forms.IntegerField(
        label="Expires after (days)", min_value=1, max_value=30, initial=7
    )

    def __init__(self, *args, can_email=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["intake_form"].queryset = IntakeForm.objects.filter(is_active=True)
        self.fields["contact"].queryset = Contact.objects.all()
        self.fields["matter"].queryset = Matter.objects.exclude(status=Matter.Status.CLOSED)
        if can_email:
            self.fields["send_email"] = forms.BooleanField(
                label="Email the link to the contact",
                required=False,
                initial=True,
                help_text="Sent through the firm's email delivery service. The next page also "
                "shows the link to copy.",
            )

    def clean(self):
        cleaned = super().clean()
        matter = cleaned.get("matter")
        contact = cleaned.get("contact")
        if matter and contact and matter.primary_contact_id != contact.id:
            is_party = matter.parties.filter(contact=contact).exists()
            if not is_party:
                raise forms.ValidationError("The selected contact is not linked to that matter.")
        if cleaned.get("send_email") and contact and address_problem(contact):
            self.add_error(
                "send_email", f"{address_problem(contact)} Or untick this to copy the link instead."
            )
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
    email = forms.EmailField(
        required=False,
        help_text="Optional updated email address",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    phone = forms.CharField(
        required=False,
        max_length=40,
        help_text="Optional updated phone number",
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
    )

    def __init__(self, *args, intake_form, **kwargs):
        super().__init__(*args, **kwargs)
        self.intake_form = intake_form
        for question in intake_form.questions:
            self.fields[question["key"]] = forms.CharField(
                label=question["label"],
                required=False,
                max_length=5000,
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
            "require_two_factor",
        ]
        widgets = {"phone": forms.TextInput(attrs={"type": "tel"})}
        labels = {
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "region": "State or region",
        }
