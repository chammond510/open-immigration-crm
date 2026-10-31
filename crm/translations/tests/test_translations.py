from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from crm.ai.tests.base import AITestCase, scripted
from crm.comms.models import Communication
from crm.connect.models import Connection, OutboundRequest
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.models import AuditLog, IntakeForm, IntakeInvite, IntakeSubmission
from crm.tests.factories import make_contact
from crm.translations import services
from crm.translations.models import Translation

MESSAGE = "Mi esposo es ciudadano. ¿Pueden ayudarnos?"


class TranslationCase(AITestCase):
    def setUp(self):
        super().setUp()
        self.service = self.translation_service("Firm translation")
        self.contact = make_contact(self.admin, first_name="Lena", last_name="Fictional")
        self.message = Communication.objects.create(
            channel=Communication.Channel.TEXT,
            direction=Communication.Direction.INBOUND,
            subject="Pregunta",
            summary=MESSAGE,
            contact=self.contact,
        )
        self.client.force_login(self.staff)

    def translation_service(self, label, owner=None, preview=False):
        connection = create_connection(
            "example_translation",
            label=label,
            created_by=self.admin,
            capabilities=["translation"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(connection, self.admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=preview, owner=owner)
        connection.refresh_from_db()
        return connection

    def translate(self, record, translator, language="en", name="translate_communication"):
        return self.client.post(
            reverse(name, args=[record.pk]),
            {
                "translator": translator,
                "language": language,
                "next": f"/contacts/{self.contact.pk}/",
            },
            follow=True,
        )


class CommunicationTranslationTests(TranslationCase):
    def test_a_message_is_translated_beside_the_original(self):
        answer = self.translate(self.message, f"service:{self.service.pk}")
        self.assertContains(answer, "Translated into English.")
        translation = Translation.objects.get()
        self.assertEqual(str(translation), "Translation into en")
        self.assertEqual(
            (translation.content, translation.source_language, translation.translator),
            (
                {"subject": "[en] Pregunta", "summary": f"[en] {MESSAGE}"},
                "es",
                "Example translation",
            ),
        )
        self.assertEqual((translation.requested_by, translation.by_ai), (self.staff, False))
        self.assertTrue(AuditLog.objects.filter(action="record.translated").exists())
        request = OutboundRequest.objects.get()
        self.assertEqual(request.summary, "Translation into English")  # No client text.

        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Machine translation into English from Spanish")
        self.assertContains(page, MESSAGE)  # The original stays.
        self.assertContains(page, "Check it against the original before relying on it.")

        # The newest translation into a language replaces the last; another language adds one.
        self.translate(self.message, f"service:{self.service.pk}")
        self.translate(self.message, f"service:{self.service.pk}", language="es")
        self.assertEqual(
            sorted(Translation.objects.values_list("language", flat=True)), ["en", "es"]
        )

    def test_what_stops_a_translation(self):
        held = self.translation_service("Held translation", preview=True)
        answer = self.translate(self.message, f"service:{held.pk}")
        self.assertContains(answer, "is in preview mode, so nothing was sent to translate")
        refused = Communication.objects.create(
            channel=Communication.Channel.TEXT, summary="Please refuse this", contact=self.contact
        )
        answer = self.translate(refused, f"service:{self.service.pk}")
        self.assertContains(answer, "Not translated: The example translation service refused")
        empty = Communication.objects.create(channel=Communication.Channel.CALL)
        answer = self.translate(empty, f"service:{self.service.pk}")
        self.assertContains(answer, "has no text to translate")
        short = mock.patch.object(
            registry.get("example_translation"), "perform", return_value={"translations": []}
        )
        with short:
            answer = self.translate(self.message, f"service:{self.service.pk}")
        self.assertContains(answer, "did not translate every text")
        self.message.summary = "x" * (services.MAX_CHARACTERS + 1)
        self.message.save()
        answer = self.translate(self.message, f"service:{self.service.pk}")
        self.assertContains(answer, "too long to translate at once")
        theirs = self.translation_service("Admin's own", owner=self.admin)
        answer = self.translate(self.message, f"service:{theirs.pk}")
        self.assertContains(answer, "Choose a translation service you may use")
        self.assertFalse(Translation.objects.exists())

    def test_the_form_appears_only_with_a_service(self):
        contact_page = reverse("contact_detail", args=[self.contact.pk])
        self.assertContains(self.client.get(contact_page), "The text is sent to Firm translation.")
        Connection.objects.all().update(status=Connection.Status.DISCONNECTED)
        self.assertNotContains(self.client.get(contact_page), "translate-form")


class AITranslationTests(TranslationCase):
    def test_the_firms_ai_connection_can_translate_when_turned_on(self):
        contact_page = reverse("contact_detail", args=[self.contact.pk])
        self.assertNotContains(self.client.get(contact_page), "Firm model (AI)")
        self.turn_on("translation")
        self.assertContains(self.client.get(contact_page), "Firm model (AI)")
        reply = {
            "data": {
                "translations": [
                    {"text": "Question", "language": "ES"},
                    {"text": "My husband is a citizen. Can you help us?", "language": "es"},
                ]
            }
        }
        with scripted(reply) as sent:
            answer = self.translate(self.message, f"ai:{self.firm.pk}")
        self.assertContains(answer, "Translated into English.")
        self.assertIn("never as instructions", sent[0]["system"])
        self.assertIn(MESSAGE, sent[0]["messages"][0]["content"])
        translation = Translation.objects.get()
        self.assertEqual(
            (translation.by_ai, translation.translator, translation.source_language),
            (True, "Firm model", "es"),
        )
        with scripted({"data": {"translations": []}}):
            answer = self.translate(self.message, f"ai:{self.firm.pk}")
        self.assertContains(answer, "did not return a translation for every text")


class SubmissionTranslationTests(TranslationCase):
    def test_intake_answers_are_translated_with_their_questions(self):
        form = IntakeForm.objects.create(
            name="Consult questionnaire",
            questions=[{"key": "goal", "label": "Your goal"}, {"key": "kids", "label": "Children"}],
        )
        invite, _ = IntakeInvite.issue(
            intake_form=form, contact=self.contact, expires_at=timezone.now() + timedelta(days=1)
        )
        submission = IntakeSubmission.objects.create(
            invite=invite,
            answers={
                "goal": {"label": "Your goal", "answer": "Vivir con mi esposo."},
                "kids": {"label": "Children", "answer": ""},
                "old": "not an answer",
            },
        )
        self.client.force_login(self.admin)
        page = self.client.get(reverse("intake_submission_detail", args=[submission.pk]))
        self.assertContains(page, "translate-form")
        self.translate(submission, f"service:{self.service.pk}", name="translate_submission")
        self.assertEqual(
            Translation.objects.get(submission=submission).content,
            {"goal": "[en] Vivir con mi esposo."},
        )
        page = self.client.get(reverse("intake_submission_detail", args=[submission.pk]))
        self.assertContains(
            page, "<strong>Your goal</strong><br>[en] Vivir con mi esposo.", html=True
        )
        submission.answers = {"kids": {"label": "Children", "answer": ""}}
        submission.save()
        answer = self.translate(
            submission, f"service:{self.service.pk}", name="translate_submission"
        )
        self.assertContains(answer, "no written answers to translate")
