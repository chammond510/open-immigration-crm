from django import forms

from .models import Authority
from .services import research_connections, scopes


def search_targets(user):
    """Where a staff member can search, as (value, label): each connection and its scopes."""
    targets = []
    for connection in research_connections(user):
        mine = " (yours)" if connection.owner_id else ""
        preview = " · preview mode" if connection.preview_mode else ""
        options = scopes(connection) or [("", "")]
        for value, label in options:
            name = f"{connection.label}{mine}" + (f" · {label}" if label else "")
            targets.append((f"{connection.pk}:{value}", f"{name}{preview}"))
    return targets


class SearchForm(forms.Form):
    query = forms.CharField(
        label="Search for authorities",
        max_length=500,
        widget=forms.TextInput(
            attrs={"placeholder": "For example: exceptional hardship to a U.S. citizen child"}
        ),
    )
    target = forms.ChoiceField(label="Search in")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections = {str(c.pk): c for c in research_connections(user)}
        self.fields["target"].choices = search_targets(user)

    def clean(self):
        cleaned = super().clean()
        connection_pk, _, scope = (cleaned.get("target") or "").partition(":")
        connection = self.connections.get(connection_pk)
        if connection is None:
            raise forms.ValidationError("Choose a legal research connection you may use.")
        cleaned["connection"], cleaned["scope"] = connection, scope
        return cleaned


class AuthorityForm(forms.ModelForm):
    """An authority added by hand, without a research connection."""

    class Meta:
        model = Authority
        fields = ["citation", "title", "court", "decided", "url", "note"]
        widgets = {
            "decided": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"title": "Case or document name", "decided": "Date decided", "url": "Link"}

    def clean_url(self):
        url = self.cleaned_data["url"]
        if url and not url.startswith("https://"):
            raise forms.ValidationError("Use an https:// link.")
        return url
