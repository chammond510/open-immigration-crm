from django.apps import AppConfig


class CustomFieldsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.customfields"
    label = "customfields"
    verbose_name = "Custom fields"

    def ready(self):
        from crm.connect import lookups

        from .models import CustomField

        def lead_source_fields():
            """Contact choice fields a booking's tracking value can fill, as for lead source."""
            return CustomField.objects.filter(
                applies_to=CustomField.AppliesTo.CONTACT,
                field_type=CustomField.FieldType.CHOICE,
                is_active=True,
            ).values_list("key", "label")

        lookups.provide_options("lead_source_fields", lead_source_fields)
