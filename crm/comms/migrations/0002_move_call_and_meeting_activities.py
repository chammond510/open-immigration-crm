"""Move activities logged as calls or meetings into the communications log.

Activity no longer offers those kinds; each one becomes a Communication with the
same subject, details, time, contact, matter, and author. Reversible.
"""

from django.db import migrations

MOVED_KINDS = {"call": "call", "meeting": "meeting"}


def move_to_communications(apps, schema_editor):
    Activity = apps.get_model("crm", "Activity")
    Communication = apps.get_model("comms", "Communication")
    for activity in Activity.objects.filter(kind__in=MOVED_KINDS).iterator():
        Communication.objects.create(
            channel=MOVED_KINDS[activity.kind],
            occurred_at=activity.occurred_at,
            subject=activity.subject,
            summary=activity.body,
            contact_id=activity.contact_id,
            matter_id=activity.matter_id,
            logged_by_id=activity.created_by_id,
        )
        activity.delete()


def move_back_to_activities(apps, schema_editor):
    Activity = apps.get_model("crm", "Activity")
    Communication = apps.get_model("comms", "Communication")
    manual = Communication.objects.filter(
        channel__in=MOVED_KINDS.values(), source_connection__isnull=True
    )
    for communication in manual.iterator():
        Activity.objects.create(
            kind=communication.channel,
            occurred_at=communication.occurred_at,
            subject=communication.subject or communication.get_channel_display(),
            body=communication.summary,
            contact_id=communication.contact_id,
            matter_id=communication.matter_id,
            created_by_id=communication.logged_by_id,
        )
        communication.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("comms", "0001_communications"),
        ("crm", "0005_texting_opt_out_and_activity_kinds"),
    ]

    operations = [migrations.RunPython(move_to_communications, move_back_to_activities)]
