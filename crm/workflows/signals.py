from django.db.models.signals import post_save

from crm.models import Matter

from .services import apply_automatic_workflow


def matter_created(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        apply_automatic_workflow(instance)


def connect():
    post_save.connect(matter_created, sender=Matter, dispatch_uid="workflows-apply-automatic")
