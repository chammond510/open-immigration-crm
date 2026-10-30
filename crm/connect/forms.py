from django import forms

from .base import ApiKeyAuth, Capability, ConnectionScope

CREDENTIAL_PREFIX = "credential_"
SETTING_PREFIX = "setting_"


class ConnectForm(forms.Form):
    """Built from a connector's manifest: capabilities, credential fields, and acknowledgment."""

    label = forms.CharField(
        max_length=120, help_text="A name staff will recognize, such as Main office line."
    )
    capabilities = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple)
    acknowledge = forms.BooleanField(
        label="I reviewed what this connection sends and receives, and the firm accepts it.",
    )

    def __init__(self, *args, connector, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.connector = connector
        self.fields["label"].initial = connector.name
        self.fields["capabilities"].choices = [
            (key, Capability(key).label) for key in sorted(connector.capabilities)
        ]
        self.fields["capabilities"].initial = sorted(connector.capabilities)
        if isinstance(connector.auth, ApiKeyAuth):
            for credential in connector.auth.fields:
                # A generated value is shown so staff can copy it to the provider.
                widget = (
                    forms.PasswordInput(attrs={"autocomplete": "off"})
                    if credential.secret and not credential.generate
                    else forms.TextInput(attrs={"autocomplete": "off"})
                )
                self.fields[f"{CREDENTIAL_PREFIX}{credential.name}"] = forms.CharField(
                    label=credential.label,
                    help_text=credential.help_text,
                    widget=widget,
                    max_length=4000,
                    initial=credential.generate() if credential.generate else None,
                )
        for setting in connector.settings_fields:
            name = f"{SETTING_PREFIX}{setting.name}"
            if setting.choices:
                self.fields[name] = forms.ChoiceField(choices=setting.choices)
            else:
                self.fields[name] = forms.CharField(max_length=500, required=setting.required)
            self.fields[name].label = setting.label
            self.fields[name].help_text = setting.help_text
            self.fields[name].initial = setting.default
            if setting.validate:
                self.fields[name].validators.append(setting.validate)
        # Only a superuser can accept a connection's data flows for the firm, and
        # the acknowledgment comes last, after everything being agreed to.
        if user.is_superuser:
            self.fields["acknowledge"] = self.fields.pop("acknowledge")
        else:
            del self.fields["acknowledge"]
        self.personal = connector.connection_scope == ConnectionScope.USER

    def credentials(self):
        return {
            name[len(CREDENTIAL_PREFIX) :]: value.strip()
            for name, value in self.cleaned_data.items()
            if name.startswith(CREDENTIAL_PREFIX)
        }

    def settings(self):
        return {
            name[len(SETTING_PREFIX) :]: value.strip()
            for name, value in self.cleaned_data.items()
            if name.startswith(SETTING_PREFIX)
        }


class ConnectorAppForm(forms.Form):
    client_id = forms.CharField(label="Client ID", max_length=255)
    client_secret = forms.CharField(
        label="Client secret",
        max_length=4000,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )

    def __init__(self, *args, connector=None, **kwargs):
        super().__init__(*args, **kwargs)
        for setting in getattr(connector.auth, "app_fields", ()) if connector else ():
            field = forms.CharField(
                max_length=255, label=setting.label, help_text=setting.help_text
            )
            field.initial = (self.initial or {}).get(setting.name, setting.default)
            if setting.validate:
                field.validators.append(setting.validate)
            self.fields[f"{SETTING_PREFIX}{setting.name}"] = field

    def app_config(self):
        return {
            name[len(SETTING_PREFIX) :]: value.strip()
            for name, value in self.cleaned_data.items()
            if name.startswith(SETTING_PREFIX)
        }


class ConnectionSettingsForm(forms.Form):
    preview_mode = forms.BooleanField(
        required=False,
        label="Preview mode: record outbound actions without sending them",
    )
    outbound_admin_only = forms.BooleanField(
        required=False,
        label="Only superusers can send through this connection by hand",
    )


class ProviderSecretForm(forms.Form):
    provider_secret = forms.CharField(
        label="Signing secret from the provider",
        max_length=4000,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )
