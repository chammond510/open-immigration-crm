from django import forms

from .services import MAX_SIGNERS, PDF, signature_connections, signer_choices


class SendForm(forms.Form):
    document = forms.ChoiceField(label="Document")
    title = forms.CharField(
        label="Title signers see",
        max_length=255,
        required=False,
        help_text="Left blank, the document's title.",
    )
    signers = forms.MultipleChoiceField(label="Signers", widget=forms.CheckboxSelectMultiple)
    message = forms.CharField(
        label="Message to signers",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    connection = forms.ChoiceField(label="Send through")

    def __init__(self, *args, matter, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.documents = {str(d.pk): d for d in matter.documents.filter(mime_type=PDF)}
        self.contacts = {str(c.pk): c for c in signer_choices(matter)}
        self.connections = {str(c.pk): c for c in signature_connections(user)}
        self.fields["document"].choices = [(k, d.title) for k, d in self.documents.items()]
        self.fields["signers"].choices = [(k, f"{c} ({c.email})") for k, c in self.contacts.items()]
        self.fields["connection"].choices = [(k, c.label) for k, c in self.connections.items()]

    def clean_signers(self):
        chosen = self.cleaned_data["signers"]
        if len(chosen) > MAX_SIGNERS:
            raise forms.ValidationError(f"Choose up to {MAX_SIGNERS} signers.")
        return [self.contacts[key] for key in chosen]

    def clean(self):
        cleaned = super().clean()
        if "document" in cleaned:
            cleaned["document"] = self.documents[cleaned["document"]]
        if "connection" in cleaned:
            cleaned["connection"] = self.connections[cleaned["connection"]]
        return cleaned
