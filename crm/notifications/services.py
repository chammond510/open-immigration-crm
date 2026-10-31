import contextvars
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.signals import post_init, post_save
from django.urls import reverse
from django.utils import timezone
from django_tasks import task

from crm.comms import system_email
from crm.models import Contact, FirmProfile, Matter, WorkItem

from .models import Notification, Preference

# The request being handled, so a change is credited to the person who made it.
current_request = contextvars.ContextVar("notifications_request", default=None)
# Set while saving many records at once, such as an import.
quiet = contextvars.ContextVar("notifications_quiet", default=False)
# An assignee that was not loaded, so whether it changed is unknown.
UNKNOWN = object()


class RequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            current_request.reset(token)


def current_actor():
    """The staff member whose request is being handled, if any."""
    user = getattr(current_request.get(), "user", None)
    if user is None or not (user.is_authenticated and user.is_active and user.is_staff):
        return None
    return user


@contextmanager
def quietly():
    token = quiet.set(True)
    try:
        yield
    finally:
        quiet.reset(token)


def display_name(user):
    return user.get_full_name() or user.get_username()


def describe(record):
    """(kind, words for the CRM, words for an email that name no client, link)."""
    if isinstance(record, WorkItem):
        kind = record.get_kind_display().lower()
        noun = f"{'an' if kind[0] in 'aeiou' else 'a'} {kind}"
        when = ""
        if record.scheduled_for:
            moment = timezone.localtime(record.scheduled_for)
            when = f" for {moment:%B} {moment.day}"
        return (
            "work_item",
            f"{noun}: {record.title}",
            noun + when,
            reverse("work_update", args=[record.pk]),
        )
    if isinstance(record, Matter):
        noun = f"matter {record.matter_number}"
        return "matter", f"{noun}: {record.title}", noun, reverse("matter_detail", args=[record.pk])
    return (
        "contact",
        f"a contact: {record.full_name}",
        "a contact",
        reverse("contact_detail", args=[record.pk]),
    )


def notify(recipient, *, actor, kind, text, brief, url, target_type="", target_id=""):
    """Tell ``recipient`` in the CRM, and by email if they asked and the firm can send it.

    ``brief`` is the email's text, so it names no client.
    """
    notification = Notification.objects.create(
        recipient=recipient,
        actor=actor,
        kind=kind,
        target_type=target_type,
        target_id=target_id,
        text=text[:255],
        url=url,
    )
    request = current_request.get()
    if (
        request is not None
        and Preference.for_user(recipient).email_assignments
        and system_email.sender() is not None
    ):
        site_url = request.build_absolute_uri("/").rstrip("/")
        transaction.on_commit(lambda: email_notification.enqueue(notification.pk, brief, site_url))
    return notification


def remember_assignee(sender, instance, **kwargs):
    instance._loaded_assignee = instance.__dict__.get("assigned_to_id", UNKNOWN)


def assignment_saved(sender, instance, created, raw=False, **kwargs):
    before = None if created else getattr(instance, "_loaded_assignee", UNKNOWN)
    after = instance.assigned_to_id
    instance._loaded_assignee = after
    if raw or quiet.get() or after is None or before is UNKNOWN or after == before:
        return
    actor = current_actor()
    if actor is None or actor.pk == after or not instance.assigned_to.is_active:
        return
    target_type, text, brief, url = describe(instance)
    notify(
        instance.assigned_to,
        actor=actor,
        kind=Notification.Kind.ASSIGNED,
        text=f"{display_name(actor)} assigned you {text}",
        brief=f"{display_name(actor)} assigned you {brief}",
        url=url,
        target_type=target_type,
        target_id=str(instance.pk),
    )


def connect():
    for model in (Contact, Matter, WorkItem):
        name = model.__name__
        post_init.connect(remember_assignee, sender=model, dispatch_uid=f"notify-load-{name}")
        post_save.connect(assignment_saved, sender=model, dispatch_uid=f"notify-save-{name}")


@task()
def email_notification(notification_id, brief, site_url):
    notification = (
        Notification.objects.select_related("recipient").filter(pk=notification_id).first()
    )
    if notification is None or not notification.recipient.is_active:
        return
    link = site_url + reverse("notification_open", args=[notification.pk])
    try:
        system_email.email_staff(
            notification.recipient,
            subject=f"{FirmProfile.load().name}: {brief}",
            body=(
                f"{brief}.\n\nOpen it in the CRM:\n\n{link}\n\n"
                "You get these emails because you turned them on in your profile, where you "
                "can turn them off.\n"
            ),
            purpose="Assignment alert",
        )
    except ValidationError:
        return
