"""Matching provider records to contacts and staff by phone number or email.

Phone numbers are compared in E.164 form (+15555550100), parsed with the firm's
default region for numbers written without a country code. A match must be
exact and unique; anything else is left for staff to link by hand.
"""

import phonenumbers
from django.conf import settings
from django.contrib.auth import get_user_model


def normalize_phone(value, region=None):
    """The number in E.164 form, or "" when it is not a valid phone number."""
    value = (value or "").strip()
    if not value or "@" in value:
        return ""
    try:
        number = phonenumbers.parse(value, region or settings.PHONE_DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(value):
    value = (value or "").strip().lower()
    return value if "@" in value else ""


def counterpart_key(value):
    """A normalized phone number or email, for matching a communication to a contact."""
    return normalize_email(value) or normalize_phone(value)


def match_contact(value):
    """The one contact with this phone number or email, or None if none or several match."""
    from crm.models import Contact

    key = counterpart_key(value)
    if not key:
        return None
    if "@" in key:
        matches = Contact.objects.filter(email__iexact=key)
    else:
        matches = Contact.objects.filter(phone_e164=key)
    found = list(matches[:2])
    return found[0] if len(found) == 1 else None


def match_staff(email):
    """The active staff member with this email, if exactly one."""
    email = normalize_email(email)
    if not email:
        return None
    found = list(get_user_model().objects.filter(email__iexact=email, is_active=True)[:2])
    return found[0] if len(found) == 1 else None
