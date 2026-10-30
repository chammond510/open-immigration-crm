from django.db.models.signals import post_init, post_save

from crm.models import Contact

from .models import StageChange


def remember_stage(sender, instance, **kwargs):
    # Read from __dict__ so a deferred field is never loaded just for this.
    instance._reports_stage = instance.__dict__.get("stage")


def record_stage(sender, instance, created, raw=False, **kwargs):
    before = getattr(instance, "_reports_stage", None)
    instance._reports_stage = instance.stage
    # A stage that was never loaded (a deferred field) cannot be compared.
    if raw or (not created and before in (None, instance.stage)):
        return
    StageChange.objects.create(
        contact=instance,
        from_stage="" if created else (before or ""),
        to_stage=instance.stage,
        changed_at=instance.created_at if created else instance.updated_at,
    )


def connect():
    post_init.connect(remember_stage, sender=Contact, dispatch_uid="reports-remember-stage")
    post_save.connect(record_stage, sender=Contact, dispatch_uid="reports-record-stage")
