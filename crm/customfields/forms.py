from django import forms

from .models import CustomField, active_fields

PREFIX = "cf_"


class CustomFieldsMixin:
    """Adds the firm's active custom fields to a contact or matter form.

    Mix in before ``forms.ModelForm`` and set ``custom_fields_for``. Values of
    inactive fields are kept untouched.
    """

    custom_fields_for = ""

    def add_custom_fields(self):
        stored = getattr(self.instance, "custom_fields", None) or {}
        self.custom_definitions = active_fields(self.custom_fields_for)
        for definition in self.custom_definitions:
            self.fields[PREFIX + definition.key] = definition.form_field(
                initial=stored.get(definition.key)
            )

    def clean(self):
        cleaned = super().clean()
        for definition in self.custom_definitions:
            name = PREFIX + definition.key
            if (
                definition.required
                and definition.field_type == CustomField.FieldType.YES_NO
                and cleaned.get(name) is None
            ):
                self.add_error(name, "Choose yes or no.")
        return cleaned

    def custom_values(self):
        values = dict(getattr(self.instance, "custom_fields", None) or {})
        for definition in self.custom_definitions:
            value = definition.to_storage(self.cleaned_data.get(PREFIX + definition.key))
            if value is None:
                values.pop(definition.key, None)
            else:
                values[definition.key] = value
        return values

    def save(self, commit=True):
        self.instance.custom_fields = self.custom_values()
        return super().save(commit=commit)


class CustomFieldForm(forms.ModelForm):
    class Meta:
        model = CustomField
        fields = [
            "applies_to",
            "label",
            "field_type",
            "choices",
            "help_text",
            "required",
            "position",
            "is_active",
        ]
        labels = {"applies_to": "Add to", "field_type": "Type"}
        widgets = {"choices": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Changing these would strand the values already saved.
            for name in ("applies_to", "field_type"):
                self.fields[name].disabled = True
                self.fields[name].help_text = "Fixed once the field is created."

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("field_type") == CustomField.FieldType.CHOICE:
            options = [line.strip() for line in cleaned.get("choices", "").splitlines()]
            options = [option for option in options if option]
            if not options:
                self.add_error("choices", "List at least one option, one per line.")
            elif len(options) > 100 or any(len(option) > 200 for option in options):
                self.add_error("choices", "Use at most 100 options of up to 200 characters.")
        return cleaned

    def save(self, commit=True):
        if not self.instance.pk:
            self.instance.key = CustomField.new_key(
                self.cleaned_data["applies_to"], self.cleaned_data["label"]
            )
        return super().save(commit=commit)
