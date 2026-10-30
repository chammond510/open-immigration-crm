from datetime import timedelta

from django import forms

from crm.forms import ActivityForm
from crm.models import Contact, Matter, TimeEntry

from .models import ApiSettings, ApiToken
from .scopes import ALL_SCOPES, READ, READ_ONLY, RESOURCES, WRITE, scope


def linked_record_fields(form):
    form.fields["contact"] = forms.ModelChoiceField(Contact.objects.all(), required=False)
    form.fields["matter"] = forms.ModelChoiceField(
        Matter.objects.select_related("primary_contact"), required=False
    )


def clean_links(form, cleaned, *, message):
    """Link the record before model validation, which needs a contact or matter.

    A matter's records belong to its primary contact unless another is named.
    """
    matter = cleaned.get("matter")
    if matter and not cleaned.get("contact"):
        cleaned["contact"] = matter.primary_contact
    if not cleaned.get("contact") and not matter:
        raise forms.ValidationError(message)
    form.instance.contact = cleaned["contact"]
    form.instance.matter = matter
    return cleaned


class NoteForm(ActivityForm):
    """A note through the API, which names the contact or matter it belongs to."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        linked_record_fields(self)

    def clean(self):
        cleaned = clean_links(
            self, super().clean(), message="Name the contact or matter the note is for."
        )
        matter = cleaned.get("matter")
        if matter and cleaned.get("contact") != matter.primary_contact:
            self.add_error("contact", "A matter's notes belong to its primary contact.")
        return cleaned


class TimeEntryForm(forms.ModelForm):
    """Finished time logged through the API; running timers stay in the web app."""

    duration_minutes = forms.IntegerField(required=False, min_value=1, max_value=24 * 60)

    class Meta:
        model = TimeEntry
        fields = ["started_at", "stopped_at", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The API needs an explicit start rather than the model's default of now.
        self.initial.pop("started_at", None)
        self.fields["started_at"].initial = None
        self.fields["stopped_at"].required = False
        linked_record_fields(self)

    def clean(self):
        cleaned = clean_links(
            self, super().clean(), message="Name the contact or matter the time is for."
        )
        started, stopped = cleaned.get("started_at"), cleaned.get("stopped_at")
        length = cleaned.get("duration_minutes")
        if started and not stopped and length:
            cleaned["stopped_at"] = stopped = started + timedelta(minutes=length)
        if started and not stopped:
            self.add_error("stopped_at", "Give stopped_at or duration_minutes.")
        elif started and stopped <= started:
            self.add_error("stopped_at", "stopped_at must be after started_at.")
        elif started and stopped - started > timedelta(hours=24):
            self.add_error("stopped_at", "A time entry can be at most 24 hours long.")
        return cleaned


class ApiTokenForm(forms.Form):
    name = forms.CharField(
        max_length=100, help_text="What will use this token, such as a Zap or a script."
    )
    lifetime_days = forms.TypedChoiceField(
        label="Expires after", choices=ApiToken.LIFETIME_CHOICES, coerce=int, initial=90
    )
    scopes = forms.MultipleChoiceField(
        choices=[(name, name) for name in ALL_SCOPES], widget=forms.CheckboxSelectMultiple
    )

    def scope_rows(self):
        """One row per resource for the template's read and write checkboxes."""
        chosen = set(self["scopes"].value() or [])
        rows = []
        for resource, label in RESOURCES.items():
            read, write = scope(resource, READ), scope(resource, WRITE)
            rows.append(
                {
                    "label": label,
                    "read": read,
                    "read_checked": read in chosen,
                    "write": None if resource in READ_ONLY else write,
                    "write_checked": write in chosen,
                }
            )
        return rows


class ApiSettingsForm(forms.ModelForm):
    class Meta:
        model = ApiSettings
        fields = ["enabled", "staff_tokens_allowed"]
