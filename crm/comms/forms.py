from datetime import timedelta

from django import forms

from crm.models import Contact, Document, Matter

from .models import Communication


class CommunicationForm(forms.ModelForm):
    duration_minutes = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=24 * 60,
        label="Length in minutes",
    )

    class Meta:
        model = Communication
        fields = [
            "channel",
            "direction",
            "occurred_at",
            "duration_minutes",
            "counterpart",
            "subject",
            "summary",
            "contact",
            "matter",
        ]
        labels = {
            "channel": "Type",
            "occurred_at": "When",
            "counterpart": "Phone, email, or name of the other party",
        }
        widgets = {
            "occurred_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "summary": forms.Textarea(attrs={"rows": 3}),
            "contact": forms.HiddenInput(),
            "matter": forms.HiddenInput(),
        }

    def __init__(self, *args, show_links=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = Contact.objects.all()
        self.fields["matter"].queryset = Matter.objects.select_related("primary_contact")
        self.fields["direction"].required = False
        # Type is always required; direction does not apply to every meeting.
        self.fields["channel"].choices = Communication.Channel.choices
        self.fields["direction"].choices = [("", "—"), *Communication.Direction.choices]
        if self.instance.duration:
            self.fields["duration_minutes"].initial = int(
                self.instance.duration.total_seconds() // 60
            )
        if show_links:
            self.fields["contact"].widget = forms.Select(choices=self.fields["contact"].choices)
            self.fields["matter"].widget = forms.Select(choices=self.fields["matter"].choices)

    def clean(self):
        cleaned = super().clean()
        matter = cleaned.get("matter")
        if matter and not cleaned.get("contact"):
            cleaned["contact"] = matter.primary_contact
        if not cleaned.get("contact") and not matter:
            raise forms.ValidationError("Choose the contact or matter this belongs to.")
        if matter and cleaned.get("contact") and cleaned["contact"] != matter.primary_contact:
            if not matter.parties.filter(contact=cleaned["contact"]).exists():
                self.add_error("contact", "That contact is not a party to the matter.")
        return cleaned

    def save(self, commit=True):
        communication = super().save(commit=False)
        communication.contact = self.cleaned_data.get("contact")
        minutes = self.cleaned_data.get("duration_minutes")
        communication.duration = timedelta(minutes=minutes) if minutes else None
        if commit:
            communication.save()
        return communication


class AttachmentSaveForm(forms.Form):
    """Where to save a copy of a file a contact sent: one of their matters, as a document."""

    matter = forms.ModelChoiceField(queryset=Matter.objects.none(), empty_label=None)
    title = forms.CharField(max_length=255)
    category = forms.ChoiceField(choices=Document.Category.choices)

    def __init__(self, *args, matters, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["matter"].queryset = matters
