"""Model fields whose values are encrypted at rest with FIELD_ENCRYPTION_KEYS."""

import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.lookups import IsNull


@lru_cache(maxsize=4)
def _fernet(keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(key) for key in keys])


def encrypt(value: str) -> str:
    return _fernet(tuple(settings.FIELD_ENCRYPTION_KEYS)).encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet(tuple(settings.FIELD_ENCRYPTION_KEYS)).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "A stored secret could not be decrypted with the configured FIELD_ENCRYPTION_KEYS."
        ) from exc


class EncryptedTextField(models.TextField):
    """Text stored as Fernet ciphertext. Values cannot be searched, only tested for null."""

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return value
        return encrypt(value)

    def get_lookup(self, lookup_name):
        if lookup_name != "isnull":
            return None
        return IsNull


class EncryptedJSONField(EncryptedTextField):
    """JSON-serializable data stored as Fernet ciphertext."""

    def from_db_value(self, value, expression, connection):
        value = super().from_db_value(value, expression, connection)
        if value is None:
            return value
        return json.loads(value)

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        if value is None:
            return value
        return encrypt(json.dumps(value, sort_keys=True))
