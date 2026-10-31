from datetime import datetime, timedelta
from unittest import mock

from axes.models import AccessAttempt
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect.registry import registry
from crm.models import AuditLog
from crm.team import reset
from crm.tests.factories import make_user

from .test_team import EmailExampleConnector, connect_email

NEW_PASSWORD = "Fictional-Reset-Password-58!"
SITE = "http://testserver"


class PasswordResetTests(TestCase):
    def setUp(self):
        EmailExampleConnector.sent = []
        patcher = mock.patch.dict(registry._connectors, {"example_email": EmailExampleConnector})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.person = make_user("paralegal", role="Paralegal")
        connect_email(make_user())

    def ask(self, address="paralegal@example.test", **extra):
        with mock.patch("crm.team.reset.send_links") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("password_reset"), {"email": address}, **extra)
        return response, task.enqueue

    def emailed_link(self):
        (email,) = EmailExampleConnector.sent
        self.assertEqual(email["to"], "paralegal@example.test")
        line = next(line for line in email["body"].splitlines() if "/password-reset/" in line)
        return line.strip()

    def test_the_sign_in_page_offers_it_only_with_email(self):
        self.assertContains(self.client.get(reverse("login")), "Forgot your password?")
        self.assertContains(self.client.get(reverse("password_reset")), "Email me a link")
        with mock.patch.object(reset.system_email, "sender", return_value=None):
            self.assertNotContains(self.client.get(reverse("login")), "Forgot your password?")
            page = self.client.get(reverse("password_reset"))
            self.assertContains(page, "Not available here", status_code=404)

    def test_a_link_by_email_sets_a_new_password_once(self):
        response, enqueue = self.ask("Paralegal@Example.test")
        self.assertRedirects(response, reverse("password_reset_sent"))
        key = reset.address_key("paralegal@example.test")
        enqueue.assert_called_once_with(key, SITE)
        logged = AuditLog.objects.get(action=reset.REQUESTED)
        self.assertEqual(logged.detail, key)
        self.assertNotIn("paralegal", logged.detail)

        self.client.post(reverse("login"), {"username": "paralegal", "password": "wrong-guess"})
        self.assertTrue(AccessAttempt.objects.filter(username="paralegal").exists())
        reset.send_links.call(key, SITE)
        link = self.emailed_link()
        self.assertIn("Your username is paralegal.", EmailExampleConnector.sent[0]["body"])
        page = self.client.get(link, follow=True)
        self.assertContains(page, "Choose a new password")
        done = self.client.post(
            page.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
            follow=True,
        )
        self.assertContains(done, "Your password is saved")
        self.assertTrue(AuditLog.objects.filter(action="user.password_reset").exists())
        self.assertFalse(AccessAttempt.objects.filter(username="paralegal").exists())
        signed_in = self.client.post(
            reverse("login"), {"username": "paralegal", "password": NEW_PASSWORD}
        )
        self.assertRedirects(signed_in, reverse("dashboard"))
        self.client.logout()
        page = self.client.get(link, follow=True)
        self.assertContains(page, "This link cannot be used")
        self.assertContains(page, "Ask for a new link")

    def test_unknown_and_inactive_addresses_get_the_same_answer_and_no_email(self):
        known, _ = self.ask()
        unknown, enqueue = self.ask("nobody@example.test")
        self.assertEqual((unknown.status_code, unknown["Location"]), (302, known["Location"]))
        enqueue.assert_called_once()
        reset.send_links.call(reset.address_key("nobody@example.test"), SITE)
        self.person.is_active = False
        self.person.save()
        reset.send_links.call(reset.address_key("paralegal@example.test"), SITE)
        self.assertEqual(EmailExampleConnector.sent, [])
        sent = self.client.get(reverse("password_reset_sent"))
        self.assertContains(sent, "If an account uses that address")

    def test_links_last_an_hour(self):
        reset.send_links.call(reset.address_key("paralegal@example.test"), SITE)
        link = self.emailed_link()
        # Django's token clock is naive local time.
        later = datetime.now() + reset.LINK_LIFETIME + timedelta(minutes=1)
        with mock.patch.object(reset.ResetTokenGenerator, "_now", return_value=later):
            page = self.client.get(link, follow=True)
        self.assertContains(page, "This link cannot be used")

    def test_an_invitation_link_cannot_reset_a_password(self):
        reset.send_links.call(reset.address_key("paralegal@example.test"), SITE)
        link = self.emailed_link().replace("/accounts/password-reset/", "/accounts/welcome/")
        self.assertContains(self.client.get(link, follow=True), "This link cannot be used")

    def test_each_address_gets_a_few_emails_an_hour(self):
        for _ in range(reset.EMAILS_PER_ADDRESS_PER_HOUR):
            _, enqueue = self.ask()
            enqueue.assert_called_once()
        response, enqueue = self.ask()
        self.assertRedirects(response, reverse("password_reset_sent"))
        enqueue.assert_not_called()

    def test_each_client_gets_a_few_requests_an_hour(self):
        for number in range(reset.REQUESTS_PER_CLIENT_PER_HOUR):
            self.ask(f"person{number}@example.test")
        response, enqueue = self.ask("another@example.test")
        self.assertContains(response, "Too many requests", status_code=429)
        enqueue.assert_not_called()
        # Another client is not held back, and old requests stop counting.
        response, _ = self.ask("another@example.test", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(response.status_code, 302)
        AuditLog.objects.update(created_at=timezone.now() - timedelta(hours=2))
        response, _ = self.ask("another@example.test")
        self.assertEqual(response.status_code, 302)

    def test_bad_addresses_are_asked_again(self):
        response = self.client.post(reverse("password_reset"), {"email": "not an address"})
        self.assertContains(response, "Check the email address below.")
        self.assertFalse(AuditLog.objects.filter(action=reset.REQUESTED).exists())

    def test_a_missing_sender_skips_quietly(self):
        with mock.patch.object(reset.system_email, "sender", return_value=None):
            reset.send_links.call(reset.address_key("paralegal@example.test"), SITE)
        self.assertEqual(EmailExampleConnector.sent, [])
