import json
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from crm.api.models import ApiSettings, ApiToken
from crm.customfields.models import CustomField
from crm.forms import ContactForm, MatterForm
from crm.models import AuditLog, Contact
from crm.tests.factories import make_contact, make_matter, make_user


def make_field(label, field_type="text", applies_to="contact", **fields):
    return CustomField.objects.create(
        applies_to=applies_to,
        label=label,
        key=CustomField.new_key(applies_to, label),
        field_type=field_type,
        **fields,
    )


def contact_data(**custom):
    return {
        "first_name": "Lena",
        "last_name": "Example",
        "status": "lead",
        "stage": "inquiry",
        **{f"cf_{key}": value for key, value in custom.items()},
    }


class CustomFieldModelTests(TestCase):
    def test_keys_are_unique_per_record_type(self):
        self.assertEqual(make_field("Referral source").key, "referral_source")
        self.assertEqual(make_field("Referral source!").key, "referral_source_2")
        self.assertEqual(make_field("Referral source", applies_to="matter").key, "referral_source")
        self.assertEqual(CustomField.new_key("contact", "2nd language"), "field_2nd_language")
        self.assertEqual(CustomField.new_key("contact", "¿?"), "field")
        self.assertEqual(str(make_field("Shown")), "Contacts: Shown")

    def test_storage_and_display(self):
        number = make_field("Household size", "number")
        self.assertEqual(number.to_storage(Decimal("4.000000")), 4)
        self.assertEqual(number.to_storage(Decimal("2.5")), 2.5)
        when = make_field("Entry date", "date")
        self.assertEqual(when.to_storage(date(2026, 3, 2)), "2026-03-02")
        self.assertEqual(when.display("2026-03-02"), "Mar 2, 2026")
        self.assertEqual(when.display("not a date"), "not a date")
        yes_no = make_field("Prior filing", "yes_no")
        self.assertEqual((yes_no.display(True), yes_no.display(False)), ("Yes", "No"))
        self.assertEqual(yes_no.display(None), "")
        self.assertIsNone(yes_no.to_storage(""))


class CustomFieldFormTests(TestCase):
    def setUp(self):
        self.source = make_field("Referral source", "choice", choices="Website\nReferral")
        self.size = make_field("Household size", "number")
        self.prior = make_field("Prior filing", "yes_no", required=True)

    def test_values_are_saved_on_the_record(self):
        form = ContactForm(
            contact_data(referral_source="Website", household_size="4", prior_filing="true")
        )
        self.assertTrue(form.is_valid(), form.errors)
        contact = form.save()
        self.assertEqual(
            contact.custom_fields,
            {"referral_source": "Website", "household_size": 4, "prior_filing": True},
        )

    def test_required_choices_and_kept_values(self):
        form = ContactForm(contact_data(referral_source="Billboard"))
        self.assertFalse(form.is_valid())
        self.assertIn("cf_referral_source", form.errors)
        self.assertEqual(form.errors["cf_prior_filing"], ["Choose yes or no."])

        contact = make_contact(custom_fields={"referral_source": "Radio", "old_field": "kept"})
        self.source.choices = "Website"
        self.source.save()
        form = ContactForm(instance=contact)
        self.assertIn(("Radio", "Radio"), form.fields["cf_referral_source"].choices)
        form = ContactForm(
            contact_data(referral_source="Radio", household_size="", prior_filing="false"),
            instance=contact,
        )
        self.assertTrue(form.is_valid(), form.errors)
        contact = form.save()
        self.assertEqual(
            contact.custom_fields,
            {"referral_source": "Radio", "old_field": "kept", "prior_filing": False},
        )

    def test_inactive_fields_are_hidden_and_kept(self):
        self.size.is_active = False
        self.size.save()
        matter_field = make_field("Venue", applies_to="matter")
        contact = make_contact(custom_fields={"household_size": 3})
        form = ContactForm(contact_data(prior_filing="true"), instance=contact)
        self.assertNotIn("cf_household_size", form.fields)
        self.assertNotIn("cf_venue", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().custom_fields["household_size"], 3)
        self.assertIn(f"cf_{matter_field.key}", MatterForm().fields)


class CustomFieldPageTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.client.force_login(self.admin)

    def test_superusers_define_fields(self):
        page = self.client.get(reverse("custom_field_list"))
        self.assertContains(page, "No custom fields for contacts.")
        form = self.client.get(reverse("custom_field_create"), {"for": "matter"})
        self.assertEqual(form.context["form"]["applies_to"].value(), "matter")
        response = self.client.post(
            reverse("custom_field_create"),
            {"applies_to": "matter", "label": "Venue", "field_type": "choice", "position": "0"},
        )
        self.assertContains(response, "List at least one option")
        response = self.client.post(
            reverse("custom_field_create"),
            {
                "applies_to": "matter",
                "label": "Venue",
                "field_type": "choice",
                "choices": "Fictional court A\nFictional court B",
                "position": "0",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("custom_field_list"))
        field = CustomField.objects.get()
        self.assertEqual(field.key, "venue")
        self.assertTrue(AuditLog.objects.filter(action="custom_field.created").exists())
        response = self.client.post(
            reverse("custom_field_update", args=[field.pk]),
            {
                "applies_to": "contact",
                "label": "Court",
                "field_type": "text",
                "choices": "Fictional court A",
                "position": "1",
                "is_active": "on",
            },
        )
        field.refresh_from_db()
        self.assertEqual((field.label, field.key), ("Court", "venue"))
        self.assertEqual((field.applies_to, field.field_type), ("matter", "choice"))

        self.client.force_login(make_user("paralegal", superuser=False))
        self.assertEqual(self.client.get(reverse("custom_field_list")).status_code, 403)
        self.client.force_login(make_user("office", role="Office administrator"))
        self.assertEqual(self.client.get(reverse("custom_field_list")).status_code, 200)

    def test_values_show_on_record_pages(self):
        make_field("Referral source")
        make_field("Venue", applies_to="matter")
        contact = make_contact(custom_fields={"referral_source": "Fictional website"})
        page = self.client.get(reverse("contact_detail", args=[contact.pk]))
        self.assertContains(page, "<dt>Referral source</dt><dd>Fictional website</dd>", html=True)
        matter = make_matter(contact)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, "<dt>Venue</dt><dd>—</dd>", html=True)
        edit = self.client.get(reverse("contact_update", args=[contact.pk]))
        self.assertContains(edit, 'name="cf_referral_source"')


class CustomFieldApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        ApiSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        _, self.token = ApiToken.issue(
            user=self.user, name="Fictional", scopes=["contacts:write"], lifetime_days=30
        )
        make_field("Referral source", "choice", choices="Website\nReferral")
        make_field("Household size", "number")

    def api(self, method, path, data=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        call = getattr(self.client, method)
        if data is None:
            return call(path, headers=headers)
        return call(path, json.dumps(data), content_type="application/json", headers=headers)

    def test_reading_and_writing_custom_fields(self):
        index = self.api("get", "/api/v1/").json()
        fields = {field["key"]: field for field in index["custom_fields"]["contact"]}
        # Fields at the same position are listed alphabetically.
        self.assertEqual(list(fields), ["household_size", "referral_source"])
        self.assertEqual(fields["referral_source"]["choices"], ["Website", "Referral"])
        self.assertEqual(index["custom_fields"]["matter"], [])
        response = self.api(
            "post",
            "/api/v1/contacts/",
            {
                "first_name": "Lena",
                "last_name": "Example",
                "custom_fields": {"referral_source": "Website", "household_size": 3},
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["custom_fields"], {"referral_source": "Website", "household_size": 3})
        response = self.api(
            "patch", f"/api/v1/contacts/{body['id']}/", {"custom_fields": {"household_size": 5}}
        )
        self.assertEqual(
            response.json()["custom_fields"], {"referral_source": "Website", "household_size": 5}
        )
        response = self.api(
            "patch", f"/api/v1/contacts/{body['id']}/", {"custom_fields": {"household_size": None}}
        )
        self.assertEqual(response.json()["custom_fields"], {"referral_source": "Website"})
        self.assertEqual(Contact.objects.get().custom_fields, {"referral_source": "Website"})

    def test_custom_field_errors(self):
        contact = make_contact()
        url = f"/api/v1/contacts/{contact.pk}/"
        response = self.api("patch", url, {"custom_fields": {"referral_source": "Billboard"}})
        self.assertIn("custom_fields.referral_source", response.json()["error"]["fields"])
        response = self.api("patch", url, {"custom_fields": {"unknown": "x"}})
        self.assertEqual(response.json()["error"]["fields"], ["custom_fields.unknown"])
        response = self.api("patch", url, {"custom_fields": ["Website"]})
        self.assertIn("custom_fields", response.json()["error"]["fields"])
        response = self.api("patch", url, {"cf_referral_source": "Website"})
        self.assertEqual(response.json()["error"]["fields"], ["cf_referral_source"])
        response = self.api("patch", url, {"custom_fields": {"household_size": [1]}})
        self.assertIn("custom_fields.household_size", response.json()["error"]["message"])
