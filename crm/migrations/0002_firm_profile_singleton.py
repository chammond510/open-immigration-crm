from django.db import migrations


def create_firm_profile(apps, schema_editor):
    firm_profile = apps.get_model("crm", "FirmProfile")
    firm_profile.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("crm", "0001_initial")]

    operations = [migrations.RunPython(create_firm_profile, migrations.RunPython.noop)]
