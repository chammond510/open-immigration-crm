from django import forms

from .services import LANGUAGES, translators


class TranslateForm(forms.Form):
    translator = forms.ChoiceField(label="Translate with")
    language = forms.ChoiceField(label="Into", choices=LANGUAGES, initial="en")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.options = {option.key: option for option in translators(user)}
        self.fields["translator"].choices = [
            (option.key, option.label) for option in self.options.values()
        ]

    def chosen(self):
        return self.options[self.cleaned_data["translator"]]
