"""Authenticator-app codes (RFC 6238 TOTP) and recovery codes for staff sign-in."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

import segno
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import FirmProfile, RecoveryCode, TwoFactorDevice

PERIOD_SECONDS = 30
DIGITS = 6
# Accept the previous and next time step to tolerate phone clock drift.
DRIFT_STEPS = 1
RECOVERY_CODE_COUNT = 10
# Base32 without 0/O/1/I so codes survive being read aloud or handwritten.
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_LENGTH = 10

SESSION_PENDING_KEY = "two_factor_pending"
SESSION_VERIFIED_KEY = "two_factor_verified"

ACCEPTED = "accepted"
ACCEPTED_RECOVERY = "accepted_recovery"
REJECTED = "rejected"
LOCKED = "locked"


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_counter(now=None):
    return int((time.time() if now is None else now) // PERIOD_SECONDS)


def match_counter(secret, code, *, after, now=None):
    """Return the time step a fresh code belongs to, or None.

    Steps at or before `after` are rejected so a code cannot be replayed.
    """
    code = "".join(code.split())
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):
        return None
    counter = current_counter(now)
    for candidate in range(counter - DRIFT_STEPS, counter + DRIFT_STEPS + 1):
        if candidate > after and hmac.compare_digest(hotp(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret, *, account, issuer):
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def qr_code_data_uri(uri):
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


def format_secret(secret):
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def normalize_recovery_code(raw):
    return "".join(raw.split()).replace("-", "").upper()


def hash_recovery_code(raw):
    return hashlib.sha256(normalize_recovery_code(raw).encode()).hexdigest()


def generate_recovery_codes():
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        code = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        codes.append(f"{code[:5]}-{code[5:]}")
    return codes


def confirmed_device(user):
    return TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=False).first()


def firm_requires_two_factor():
    return FirmProfile.objects.filter(pk=1, require_two_factor=True).exists()


def issuer_name():
    firm = FirmProfile.objects.filter(pk=1).first()
    if firm is None:
        return "Open Immigration CRM"
    return firm.short_name or firm.name


@transaction.atomic
def replace_recovery_codes(user):
    """Invalidate every existing recovery code and return a fresh set to show once."""
    RecoveryCode.objects.filter(user=user).delete()
    codes = generate_recovery_codes()
    RecoveryCode.objects.bulk_create(
        RecoveryCode(user=user, code_hash=hash_recovery_code(code)) for code in codes
    )
    return codes


def remaining_recovery_codes(user):
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


@transaction.atomic
def confirm_device(device, code):
    """Confirm a pending device with its first code; return new recovery codes or None."""
    device = TwoFactorDevice.objects.select_for_update().get(pk=device.pk)
    counter = match_counter(device.secret, code, after=0)
    if counter is None:
        return None
    device.confirmed_at = timezone.now()
    device.last_used_counter = counter
    device.save(update_fields=["confirmed_at", "last_used_counter"])
    return replace_recovery_codes(device.user)


@transaction.atomic
def check_second_factor(user, code):
    """Verify an authenticator or recovery code, enforcing replay and lockout limits."""
    device = (
        TwoFactorDevice.objects.select_for_update()
        .filter(user=user, confirmed_at__isnull=False)
        .first()
    )
    if device is None:
        return REJECTED
    now = timezone.now()
    if device.locked_until and device.locked_until > now:
        return LOCKED

    counter = match_counter(device.secret, code, after=device.last_used_counter)
    recovery = None
    if counter is None and len(normalize_recovery_code(code)) == RECOVERY_CODE_LENGTH:
        recovery = (
            RecoveryCode.objects.select_for_update()
            .filter(user=user, code_hash=hash_recovery_code(code), used_at__isnull=True)
            .first()
        )

    if counter is None and recovery is None:
        device.failed_attempts += 1
        outcome = REJECTED
        if device.failed_attempts >= settings.TWO_FACTOR_FAILURE_LIMIT:
            device.failed_attempts = 0
            device.locked_until = now + settings.AXES_COOLOFF_TIME
            outcome = LOCKED
        device.save(update_fields=["failed_attempts", "locked_until"])
        return outcome

    device.failed_attempts = 0
    device.locked_until = None
    if counter is not None:
        device.last_used_counter = counter
    device.save(update_fields=["failed_attempts", "locked_until", "last_used_counter"])
    if recovery is not None:
        recovery.used_at = now
        recovery.save(update_fields=["used_at"])
        return ACCEPTED_RECOVERY
    return ACCEPTED


@transaction.atomic
def reset_two_factor(user):
    """Remove a user's device and recovery codes so they can enroll again."""
    deleted, _ = TwoFactorDevice.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()
    return bool(deleted)
