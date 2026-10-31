from django import forms
from django.contrib.auth import get_user_model

from crm.models import Contact, Matter

from .spreadsheet import DATE_ORDERS, MAX_BYTES, SpreadsheetError, columns, read_csv

NAME_KEYS = {"full_name", "first_name", "last_name"}


class UploadForm(forms.Form):
    file = forms.FileField(label="CSV file")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        try:
            # One byte past the limit is enough to refuse a file that is too large.
            self.text, self.headers, rows = read_csv(upload.read(MAX_BYTES + 1))
        except SpreadsheetError as exc:
            raise forms.ValidationError(str(exc)) from None
        self.row_count = len(rows)
        return upload


class ColumnsForm(forms.Form):
    """Which CRM field each column fills, and the values for fields a row leaves out."""

    date_order = forms.ChoiceField(
        label="Dates in the file are written as",
        choices=[(key, label) for key, (label, _) in DATE_ORDERS.items()],
    )
    contact_status = forms.ChoiceField(
        label="Status for new contacts",
        choices=Contact.Status.choices,
        help_text="Used when a row has no contact status.",
    )
    stage = forms.ChoiceField(
        label="Stage for new contacts",
        choices=Contact.Stage.choices,
        help_text="Used when a row has no stage.",
    )
    matter_status = forms.ChoiceField(
        label="Status for new matters",
        choices=Matter.Status.choices,
        help_text="Used when a row has no matter status.",
    )
    assigned_to = forms.ModelChoiceField(
        label="Assign new contacts and matters to",
        queryset=get_user_model().objects.none(),
        required=False,
        empty_label="Nobody",
        help_text="Used when a row has no Assigned to.",
    )

    def __init__(self, *args, spreadsheet, **kwargs):
        options = spreadsheet.options or {}
        kwargs.setdefault(
            "initial",
            {
                "date_order": options.get("date_order", "mdy"),
                "contact_status": options.get("contact_status", Contact.Status.CLIENT),
                "stage": options.get("stage", Contact.Stage.ACTIVE),
                "matter_status": options.get("matter_status", Matter.Status.OPEN),
                "assigned_to": options.get("assigned_to"),
            },
        )
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(
            is_active=True, is_staff=True
        )
        available = columns()
        self.labels = {column.key: column.label for column in available}
        self.matter_keys = {column.key for column in available if column.record == "matter"}
        groups = [
            ("Contact", [(c.key, c.label) for c in available if c.record == "contact"]),
            ("Matter", [(c.key, c.label) for c in available if c.record == "matter"]),
        ]
        self.headers = spreadsheet.headers
        mapping = spreadsheet.mapping or {}
        for index, header in enumerate(self.headers):
            self.fields[f"column_{index}"] = forms.ChoiceField(
                label=header or f"Column {index + 1}",
                choices=[("", "Don't import"), *groups],
                required=False,
                initial=mapping.get(str(index), ""),
            )

    def column_fields(self):
        return [self[f"column_{index}"] for index in range(len(self.headers))]

    def option_fields(self):
        names = ("date_order", "contact_status", "stage", "matter_status", "assigned_to")
        return [self[name] for name in names]

    def clean(self):
        cleaned = super().clean()
        chosen = {}
        for index in range(len(self.headers)):
            name = f"column_{index}"
            key = cleaned.get(name)
            if not key:
                continue
            if key in chosen:
                self.add_error(
                    name,
                    f"{self.labels[key]} is already filled from "
                    f"“{self.fields[chosen[key]].label}”.",
                )
            else:
                chosen[key] = name
        if not set(chosen) & NAME_KEYS:
            raise forms.ValidationError(
                "Choose the column with each person's name: Full name, or First name and Last name."
            )
        if set(chosen) & self.matter_keys and "matter_title" not in chosen:
            raise forms.ValidationError(
                "Choose the column with each matter's title, or import no matter columns."
            )
        self.chosen = chosen
        return cleaned

    def mapping(self):
        return {name.removeprefix("column_"): key for key, name in self.chosen.items()}

    def options(self):
        assignee = self.cleaned_data["assigned_to"]
        return {
            "date_order": self.cleaned_data["date_order"],
            "contact_status": self.cleaned_data["contact_status"],
            "stage": self.cleaned_data["stage"],
            "matter_status": self.cleaned_data["matter_status"],
            "assigned_to": assignee.pk if assignee else None,
        }
