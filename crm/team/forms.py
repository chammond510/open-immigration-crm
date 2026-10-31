from django import forms
from django.contrib.auth import get_user_model

from .services import may_grant, roles

NO_ROLE = "No role (everyday work only)"


class AccountFields(forms.ModelForm):
    """A person's name and email, the email unique among accounts."""

    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.Meta.fields:
            self.fields[name].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        others = get_user_model().objects.exclude(pk=self.instance.pk)
        if others.filter(email__iexact=email).exists():
            raise forms.ValidationError("Another account already uses this email address.")
        if others.filter(username__iexact=email).exists():
            raise forms.ValidationError("Another account already signs in with this address.")
        return email

    def save(self, commit=True):
        person = super().save(commit=False)
        # Accounts made here sign in with their email, so the two move together.
        if not person.pk or person.username.lower() == self.initial.get("email", "").lower():
            person.username = person.email
        if commit:
            person.save()
        return person


class PersonForm(AccountFields):
    """Add a person, or change their name, email, and role."""

    role = forms.ModelChoiceField(queryset=None, required=False, empty_label=NO_ROLE)

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        offered = [group.pk for group in roles() if may_grant(actor, group)]
        self.fields["role"].queryset = roles().filter(pk__in=offered)
        self.fields["role"].help_text = (
            "What they may do beyond everyday work. You can give only roles whose "
            "permissions you have."
        )
        if self.instance.pk:
            self.initial.setdefault("role", self.instance.groups.first())

    def save(self, commit=True):
        person = super().save(commit=False)
        if not person.pk:
            person.is_staff = True
            person.set_unusable_password()
        if commit:
            person.save()
            role = self.cleaned_data["role"]
            person.groups.set([role] if role else [])
        return person


class ProfileForm(AccountFields):
    pass


class DeactivateForm(forms.Form):
    successor = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label="Give their open work, matters, and contacts to",
        empty_label="Nobody: leave them unassigned",
    )

    def __init__(self, *args, person, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["successor"]
        field.queryset = (
            get_user_model()
            .objects.filter(is_active=True, is_staff=True)
            .exclude(pk=person.pk)
            .order_by("first_name", "last_name", "username")
        )
        field.label_from_instance = lambda user: user.get_full_name() or user.username


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(label="Email address", max_length=254)
