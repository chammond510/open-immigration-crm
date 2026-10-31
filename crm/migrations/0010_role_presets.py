from django.db import migrations

# The preset roles as first shipped. A firm may change them afterwards, so this
# migration keeps its own copy rather than reading crm.roles.
PERMISSIONS = {
    ("crm", "firmprofile"): {
        "manage_staff": "Can manage staff",
        "manage_firm_settings": "Can manage firm settings",
        "manage_connections": "Can manage connections and AI",
        "import_records": "Can import records",
        "export_records": "Can export records",
        "delete_records": "Can delete records",
    },
    ("billing", "invoice"): {
        "access_billing": "Can view and manage billing",
        "manage_billing": "Can void billing and set rates",
    },
}
PRESETS = {
    "Managing partner": [
        "access_billing",
        "manage_billing",
        "delete_records",
        "import_records",
        "export_records",
        "manage_firm_settings",
        "manage_connections",
        "manage_staff",
    ],
    "Attorney": ["access_billing", "delete_records"],
    "Paralegal": [],
    "Office administrator": [
        "access_billing",
        "manage_billing",
        "delete_records",
        "import_records",
        "manage_firm_settings",
        "manage_staff",
    ],
}


def create_presets(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    # Permissions are normally created after every migration has run, so a new
    # installation does not have them yet.
    found = {}
    for (app_label, model), codenames in PERMISSIONS.items():
        content_type, _ = ContentType.objects.get_or_create(app_label=app_label, model=model)
        for codename, name in codenames.items():
            found[codename], _ = Permission.objects.get_or_create(
                content_type=content_type, codename=codename, defaults={"name": name}
            )
    for name, codenames in PRESETS.items():
        group, created = Group.objects.get_or_create(name=name)
        # A group the firm already made with this name is left as it is.
        if created:
            group.permissions.set([found[codename] for codename in codenames])


def remove_presets(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=PRESETS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("billing", "0005_role_permissions"),
        ("crm", "0009_role_permissions"),
    ]

    operations = [migrations.RunPython(create_presets, remove_presets)]
