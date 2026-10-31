"""Machine translation of client messages and intake answers, when staff ask.

A translation service connection declares the ``translate`` action, with the payload
``{"texts": [text, ...], "language": code}``, and answers
``{"translations": [{"text": translation, "language": the detected source code}, ...]}``,
one per text in order. Codes are lowercase, such as ``en`` or ``es``. Every request
goes through the outbound policy. With AI features on, the firm's AI connections can
translate too, through the Translation AI feature. Either way the translation is
kept beside the original, labeled as machine-made, and the newest one into each
language replaces any earlier one.
"""

from collections import Counter
from dataclasses import dataclass

from crm.ai import translation as ai_translation
from crm.ai.client import AIUnavailable, ai_connections
from crm.ai.models import AISettings
from crm.comms.sharing import withheld_reason
from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import OutboundRequest
from crm.connect.services import usable_connections

from .models import Translation

TRANSLATE = "translate"
# The languages staff can translate into. A service may not offer every one.
LANGUAGES = (
    ("en", "English"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
    ("fr", "French"),
    ("zh", "Chinese"),
    ("ar", "Arabic"),
    ("ru", "Russian"),
    ("uk", "Ukrainian"),
    ("ko", "Korean"),
    ("ja", "Japanese"),
    ("vi", "Vietnamese"),
    ("tr", "Turkish"),
    ("id", "Indonesian"),
    ("ht", "Haitian Creole"),
)
LANGUAGE_NAMES = dict(LANGUAGES)
# More than this, in all, is too much to translate at once.
MAX_CHARACTERS = 50_000


class TranslationUnavailable(Exception):
    """Why a translation could not be made, for staff."""


@dataclass(frozen=True)
class Translator:
    key: str
    label: str
    connection: object
    by_ai: bool = False


def language_name(code):
    return LANGUAGE_NAMES.get(code, code.upper())


def translators(user):
    """The translation services, and with AI translation on the AI connections, ``user`` may use."""
    found = [
        Translator(f"service:{connection.pk}", connection.label, connection)
        for connection in usable_connections(user, TRANSLATE, Capability.TRANSLATION)
    ]
    if AISettings.load().feature_on(ai_translation.FEATURE.key):
        found += [
            Translator(f"ai:{connection.pk}", f"{connection.label} (AI)", connection, by_ai=True)
            for connection in ai_connections(user)
        ]
    return found


def run(translator, texts, language, *, actor, target):
    """``(translation, source language)`` for each text, or TranslationUnavailable."""
    if sum(len(text) for text in texts) > MAX_CHARACTERS:
        raise TranslationUnavailable("This is too long to translate at once.")
    if translator.by_ai:
        try:
            return ai_translation.translate(
                texts,
                language=language_name(language),
                actor=actor,
                connection=translator.connection,
            )
        except AIUnavailable as exc:
            raise TranslationUnavailable(str(exc)) from None
    connection = translator.connection
    request = policy.send(
        connection,
        TRANSLATE,
        {"texts": texts, "language": language},
        actor=actor,
        target=target,
        summary=f"Translation into {language_name(language)}",
    )
    if request.status == OutboundRequest.Status.PREVIEWED:
        raise TranslationUnavailable(
            f"{connection.label} is in preview mode, so nothing was sent to translate. "
            "Someone who manages connections and AI can turn preview mode off on the "
            "connection's page."
        )
    if request.status != OutboundRequest.Status.SENT:
        raise TranslationUnavailable(request.error or "The text was not sent to translate.")
    items = (getattr(request, "response", None) or {}).get("translations")
    if not isinstance(items, list) or len(items) != len(texts):
        raise TranslationUnavailable(f"{connection.label} did not translate every text.")
    return [
        (str(item.get("text", "")), str(item.get("language", "")).lower()[:12]) for item in items
    ]


def save(translator, language, results, keys, *, actor, **record):
    languages = Counter(source for _, source in results if source and source != language)
    Translation.objects.filter(language=language, **record).delete()
    return Translation.objects.create(
        language=language,
        source_language=languages.most_common(1)[0][0] if languages else "",
        content={key: text for key, (text, _) in zip(keys, results, strict=True)},
        # A service is credited by name, as some, such as DeepL, require.
        translator=translator.connection.label
        if translator.by_ai
        else translator.connection.connector_class.name,
        by_ai=translator.by_ai,
        connection=translator.connection,
        requested_by=actor,
        **record,
    )


def translate_communication(communication, translator, language, *, actor):
    reason = withheld_reason(communication)
    if reason:
        raise TranslationUnavailable(
            f"{reason} Someone who manages connections and AI can allow it on the connection."
        )
    fields = [
        (name, getattr(communication, name).strip())
        for name in ("subject", "summary")
        if getattr(communication, name).strip()
    ]
    if not fields:
        raise TranslationUnavailable("This communication has no text to translate.")
    results = run(
        translator,
        [text for _, text in fields],
        language,
        actor=actor,
        target=communication.contact,
    )
    return save(
        translator,
        language,
        results,
        [name for name, _ in fields],
        actor=actor,
        communication=communication,
    )


def translate_submission(submission, translator, language, *, actor):
    answers = [
        (key, answer["answer"].strip())
        for key, answer in (submission.answers or {}).items()
        if isinstance(answer, dict)
        and isinstance(answer.get("answer"), str)
        and answer["answer"].strip()
    ]
    if not answers:
        raise TranslationUnavailable("This submission has no written answers to translate.")
    results = run(
        translator,
        [text for _, text in answers],
        language,
        actor=actor,
        target=submission.invite.contact,
    )
    return save(
        translator,
        language,
        results,
        [key for key, _ in answers],
        actor=actor,
        submission=submission,
    )
