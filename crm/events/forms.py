from django import forms

from .catalog import DETAIL_CHOICES, DETAIL_IDS, EVENT_TYPES


class EventSettingsForm(forms.Form):
    events = forms.MultipleChoiceField(
        choices=list(EVENT_TYPES.items()),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    detail = forms.ChoiceField(
        label="What each event includes",
        choices=DETAIL_CHOICES,
        initial=DETAIL_IDS,
        widget=forms.RadioSelect,
    )
