from django import forms

from crm.connect.models import Connection

from . import features
from .client import ai_connections, firm_ai_connections

FEATURE_PREFIX = "feature_"


class AISettingsForm(forms.Form):
    enabled = forms.BooleanField(
        required=False,
        label="Turn on AI features",
        help_text="While off, no AI feature runs and nothing is sent to an AI model.",
    )
    background_connection = forms.ModelChoiceField(
        queryset=Connection.objects.none(),
        required=False,
        label="Connection for background work",
        empty_label="None: background features do not run",
        help_text="Work no one is waiting on runs only through this firm-wide connection, "
        "never a staff member's own.",
    )

    def __init__(self, *args, ai_settings, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["enabled"].initial = ai_settings.enabled
        self.fields["background_connection"].queryset = Connection.objects.filter(
            pk__in=[c.pk for c in firm_ai_connections()]
        )
        self.fields["background_connection"].initial = ai_settings.background_connection_id
        for feature in features.all_features():
            self.fields[f"{FEATURE_PREFIX}{feature.key}"] = forms.BooleanField(
                required=False,
                label=feature.label,
                help_text=feature.description,
                initial=bool((ai_settings.features or {}).get(feature.key)),
            )

    def feature_values(self):
        return {
            name[len(FEATURE_PREFIX) :]: value
            for name, value in self.cleaned_data.items()
            if name.startswith(FEATURE_PREFIX)
        }


class ConnectionTestForm(forms.Form):
    connection = forms.ModelChoiceField(
        queryset=Connection.objects.none(), label="Connection", empty_label=None
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["connection"].queryset = Connection.objects.filter(
            pk__in=[c.pk for c in ai_connections(user)]
        )
