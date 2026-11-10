from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mcp", "0001_mcp_settings_and_tool_calls"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mcpsettings",
            name="write_tools_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Assistants may add tasks, notes, communications, status checks, and saved "
                    "legal authorities, after the staff member confirms each one. Nothing can be "
                    "deleted."
                ),
                verbose_name="Allow write tools",
            ),
        ),
    ]
