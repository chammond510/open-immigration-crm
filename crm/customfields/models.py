"""Fields a firm adds to contacts and matters.

Values live on the record itself (``Contact.custom_fields`` and
``Matter.custom_fields``), keyed by each field's ``key``, so exports, the API,
and webhook payloads carry them without extra joins.
"""

import re
from datetime import date
from decimal import Decimal

from django import forms
from django.db import models
from django.utils.dateparse import parse_date
from django.utils.text import slugify

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,59}$")


class CustomField(models.Model):
    class AppliesTo(models.TextChoices):
        CONTACT = "contact", "Contacts"
        MATTER = "matter", "Matters"

    class FieldType(models.TextChoices):
        TEXT = "text", "Short text"
        LONG_TEXT = "long_text", "Long text"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        CHOICE = "choice", "Choice from a list"
        YES_NO = "yes_no", "Yes or no"

    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices)
    label = models.CharField(max_length=100)
    key = models.CharField(
        max_length=60, help_text="Set from the label when created; used in the API and exports."
    )
    field_type = models.CharField(max_length=20, choices=FieldType.choices)
    choices = models.TextField(blank=True, help_text="For a choice field, one option per line.")
    help_text = models.CharField(max_length=200, blank=True)
    required = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(
        default=True, help_text="Inactive fields are hidden; their saved values are kept."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["applies_to", "position", "label"]
        constraints = [
            models.UniqueConstraint(fields=["applies_to", "key"], name="customfields_unique_key")
        ]

    def __str__(self):
        return f"{self.get_applies_to_display()}: {self.label}"

    @property
    def choice_list(self):
        return [line.strip() for line in self.choices.splitlines() if line.strip()]

    @classmethod
    def new_key(cls, applies_to, label):
        base = slugify(label).replace("-", "_")[:50] or "field"
        if not base[0].isalpha():
            base = f"field_{base}"[:50]
        key, number = base, 2
        while cls.objects.filter(applies_to=applies_to, key=key).exists():
            key, number = f"{base}_{number}", number + 1
        return key

    def form_field(self, initial=None):
        options = {
            "label": self.label,
            "help_text": self.help_text,
            "required": self.required,
            "initial": initial,
        }
        if self.field_type == self.FieldType.LONG_TEXT:
            return forms.CharField(
                max_length=5000, widget=forms.Textarea(attrs={"rows": 3}), **options
            )
        if self.field_type == self.FieldType.NUMBER:
            return forms.DecimalField(max_digits=18, decimal_places=6, **options)
        if self.field_type == self.FieldType.DATE:
            return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **options)
        if self.field_type == self.FieldType.CHOICE:
            values = self.choice_list
            # A value saved before its option was removed stays selectable.
            if initial not in (None, "") and initial not in values:
                values = [*values, initial]
            return forms.ChoiceField(choices=[("", "—"), *((v, v) for v in values)], **options)
        if self.field_type == self.FieldType.YES_NO:
            # Required is checked by the form, since "unknown" is a valid choice to render.
            options["required"] = False
            return forms.NullBooleanField(**options)
        return forms.CharField(max_length=500, **options)

    def to_storage(self, value):
        """A cleaned form value as JSON, or None for no value."""
        if value is None or value == "":
            return None
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, date):
            return value.isoformat()
        return value

    def display(self, value):
        if value is None or value == "":
            return ""
        if self.field_type == self.FieldType.YES_NO:
            return "Yes" if value else "No"
        if self.field_type == self.FieldType.DATE:
            day = parse_date(str(value)) if not isinstance(value, date) else value
            return day.strftime("%b %-d, %Y") if day else str(value)
        return str(value)


def active_fields(applies_to):
    return list(CustomField.objects.filter(applies_to=applies_to, is_active=True))
