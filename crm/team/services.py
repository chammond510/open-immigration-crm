from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from crm.comms import system_email
from crm.connect.models import Connection
from crm.connect.services import disconnect
from crm.models import Contact, FirmProfile, Matter, TimeEntry, WorkItem
from crm.notifications.models import Notification
from crm.notifications.services import display_name, notify, quietly
from crm.roles import PRESET_ROLES


class InviteTokenGenerator(PasswordResetTokenGenerator):
    """Links for choosing a first password. Each stops working once a password is set."""

    key_salt = "crm.team.InviteTokenGenerator"


invite_tokens = InviteTokenGenerator()


def staff():
    """Everyone with a staff account, active people first."""
    return (
        get_user_model()
        .objects.filter(is_staff=True)
        .prefetch_related("groups")
        .order_by("-is_active", "first_name", "last_name", "username")
    )


def roles():
    """Every role, the presets first in their usual order."""
    preset_order = Case(
        *[When(name=name, then=Value(index)) for index, name in enumerate(PRESET_ROLES)],
        default=Value(len(PRESET_ROLES)),
        output_field=IntegerField(),
    )
    return Group.objects.annotate(preset_order=preset_order).order_by("preset_order", "name")


def role_of(person):
    groups = list(person.groups.all())
    return groups[0] if groups else None


def permission_names(permissions):
    return {
        f"{permission.content_type.app_label}.{permission.codename}"
        for permission in permissions.select_related("content_type")
    }


def held_permissions(person):
    """What ``person``'s role and own grants give, whether or not the account is active."""
    return permission_names(
        Permission.objects.filter(Q(group__user=person) | Q(user=person)).distinct()
    )


def may_grant(actor, group):
    """Whether ``actor`` holds every permission in ``group``."""
    return actor.has_perms(permission_names(group.permissions.all()))


def may_manage(actor, person):
    """Whether ``actor`` may change ``person``: someone else, with nothing ``actor`` lacks."""
    if person.pk == actor.pk:
        return False
    if person.is_superuser:
        return actor.is_superuser
    return actor.has_perms(held_permissions(person))


def is_invited(person):
    """Added, but has not chosen a password or signed in yet."""
    return person.last_login is None and not person.has_usable_password()


def status(person):
    if not person.is_active:
        return "Deactivated"
    return "Invited" if is_invited(person) else "Active"


def counted(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def invite_days():
    return settings.PASSWORD_RESET_TIMEOUT // 86400


def invite(request, person):
    """Make a new invitation link, ending any earlier one, and email it when the firm can.

    Returns ``(link, problem)``. ``problem`` says why the email did not go out; when
    it is empty the email went, and the link is for nobody but the person invited.
    """
    person.set_unusable_password()
    person.save(update_fields=["password"])
    uid = urlsafe_base64_encode(force_bytes(person.pk))
    link = request.build_absolute_uri(
        reverse("welcome", args=[uid, invite_tokens.make_token(person)])
    )
    if system_email.sender() is None:
        return link, "no email delivery service is connected"
    firm = FirmProfile.load().name
    inviter = request.user.get_full_name() or request.user.get_username()
    try:
        sent = system_email.email_staff(
            person,
            subject=f"Your account at {firm}",
            body=(
                f"Hello {person.first_name},\n\n"
                f"{inviter} added you to {firm}'s practice workspace. Choose a password to "
                f"sign in:\n\n{link}\n\n"
                f"Your username is {person.get_username()}. The link works once and stops "
                f"working after {invite_days()} days. If you did not expect this email, you "
                "can ignore it.\n"
            ),
            purpose="Staff invitation",
            actor=request.user,
        )
    except ValidationError as exc:
        return link, " ".join(exc.messages).rstrip(".")
    return link, system_email.not_sent_reason(sent)


def open_assignments(person):
    """What ``person`` is responsible for: open work, open matters, and active contacts."""
    return {
        "work": WorkItem.objects.filter(assigned_to=person, is_completed=False),
        "matters": Matter.objects.filter(assigned_to=person).exclude(status=Matter.Status.CLOSED),
        "contacts": Contact.objects.filter(assigned_to=person).exclude(
            stage__in=[Contact.Stage.CLOSED, Contact.Stage.LOST]
        ),
    }


def personal_connections(person):
    return Connection.objects.filter(owner=person).exclude(status=Connection.Status.DISCONNECTED)


def deactivate(person, *, successor, actor):
    """Hand ``person``'s open work, matters, and contacts to ``successor``, or to nobody,
    then end their access. Returns how many of each moved.

    Records are saved one at a time so calendars and automations follow the change.
    """
    moved = {}
    with transaction.atomic(), quietly():
        for name, records in open_assignments(person).items():
            moved[name] = 0
            for record in records:
                record.assigned_to = successor
                record.save(update_fields=["assigned_to", "updated_at"])
                moved[name] += 1
        TimeEntry.objects.filter(user=person, stopped_at__isnull=True).update(
            stopped_at=timezone.now()
        )
        person.is_active = False
        person.save(update_fields=["is_active"])
    for connection in personal_connections(person):
        disconnect(connection, actor)
    if successor and successor.pk != actor.pk and any(moved.values()):
        handed = (
            f"{display_name(actor)} handed you {display_name(person)}'s open work: "
            f"{counted(moved['work'], 'work item')}, {counted(moved['matters'], 'matter')}, "
            f"{counted(moved['contacts'], 'contact')}"
        )
        notify(
            successor,
            actor=actor,
            kind=Notification.Kind.HANDED_OVER,
            text=handed,
            brief=handed,
            url=reverse("work_list") + "?owner=mine",
        )
    return moved
