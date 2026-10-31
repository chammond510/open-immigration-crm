"""Choosing a new password by email, offered only when the firm can send email.

Anyone may ask for a link by email address, and the page answers the same way
whether or not an account uses it. The request keeps only a hash of the address
and hands it to the worker, which looks for active staff with that address and
emails each a link, so neither the answer nor its timing tells who has an
account. Requests are counted in the audit log and limited per client address
and per email address. A link works once, for an hour.
"""

import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import base36_to_int, urlsafe_base64_encode
from django_tasks import task

from crm.comms import system_email
from crm.models import AuditLog, FirmProfile
from crm.services import client_ip

LINK_LIFETIME = timedelta(hours=1)
REQUESTS_PER_CLIENT_PER_HOUR = 10
EMAILS_PER_ADDRESS_PER_HOUR = 3
REQUESTED = "user.password_reset_requested"


class ResetTokenGenerator(PasswordResetTokenGenerator):
    """Links for choosing a new password, good for ``LINK_LIFETIME``."""

    key_salt = "crm.team.ResetTokenGenerator"

    def check_token(self, user, token):
        if not super().check_token(user, token):
            return False
        made = base36_to_int(token.split("-")[0])
        return self._num_seconds(self._now()) - made <= LINK_LIFETIME.total_seconds()


reset_tokens = ResetTokenGenerator()


def offered():
    return system_email.sender() is not None


def address_key(address):
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def limits_reached(request, key):
    """Whether this client, and this address, made their hour's requests already."""
    recent = AuditLog.objects.filter(
        action=REQUESTED, created_at__gte=timezone.now() - timedelta(hours=1)
    )
    return (
        recent.filter(ip_address=client_ip(request)).count() >= REQUESTS_PER_CLIENT_PER_HOUR,
        recent.filter(detail=key).count() >= EMAILS_PER_ADDRESS_PER_HOUR,
    )


@task()
def send_links(key, site_url):
    """Email a link to each active staff account whose address hashes to ``key``."""
    people = get_user_model().objects.filter(is_active=True, is_staff=True).exclude(email="")
    firm = FirmProfile.load().name
    minutes = int(LINK_LIFETIME.total_seconds() // 60)
    for person in people:
        if address_key(person.email) != key:
            continue
        uid = urlsafe_base64_encode(force_bytes(person.pk))
        path = reverse("password_reset_confirm", args=[uid, reset_tokens.make_token(person)])
        try:
            system_email.email_staff(
                person,
                subject=f"Choose a new password for {firm}",
                body=(
                    f"Hello {person.first_name or person.get_username()},\n\n"
                    f"Someone asked to choose a new password for your account at {firm}. "
                    f"Your username is {person.get_username()}. To choose one, open this "
                    f"link:\n\n{site_url}{path}\n\n"
                    f"It works once, for {minutes} minutes. If you did not ask, you can "
                    "ignore this email; your password stays the same.\n"
                ),
                purpose="Password reset",
            )
        except ValidationError:
            continue
