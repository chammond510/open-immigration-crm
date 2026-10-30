"""SMTP server: the CRM's own email to clients, such as secure intake links and payment
plan reminders, sent through the firm's mail server or an email delivery service.

Postmark, Amazon SES, SendGrid, Mailgun, and most mail providers accept SMTP, so
one connector covers them. The connection always uses TLS with the server's
certificate checked: STARTTLS (port 587, the default, which every one of those
services offers) or SSL from the start (port 465). Email goes out as plain text
from the firm's address, marked ``Auto-Submitted: auto-generated`` (RFC 3834) so
that auto-replies are not sent back, with an optional Reply-To address.

Postmark: https://postmarkapp.com/developer/user-guide/send-email-with-smtp
Amazon SES: https://docs.aws.amazon.com/ses/latest/dg/smtp-connect.html
SendGrid: https://www.twilio.com/docs/sendgrid/for-developers/sending-email/integrating-with-the-smtp-api
Mailgun: https://documentation.mailgun.com/docs/mailgun/user-manual/sending-messages/send-smtp
"""

import smtplib
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from connectors.imap_smtp import message_key, open_smtp, quit_quietly, validate_host, validate_port
from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
)
from crm.connect.registry import register

SEND = "send_system_email"


def validate_address(value):
    try:
        validate_email(value)
    except ValidationError:
        raise ValidationError("Enter an email address, such as office@example.test.") from None


@register
class SmtpConnector(Connector):
    key = "smtp"
    name = "SMTP server"
    description = (
        "Let the CRM email clients itself, such as secure intake links and payment plan "
        "reminders, through the firm's mail server or an email delivery service."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.SYSTEM_EMAIL})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "username",
                "Sign-in name",
                help_text="From the mail server or service's SMTP settings. For SendGrid, "
                "it is apikey.",
            ),
            CredentialField(
                "password",
                "Password",
                help_text="The SMTP password, API key, or app password the service gives "
                "for sending. A staff member's usual password should not be used.",
            ),
        ),
        label="SMTP sign-in",
    )
    settings_fields = (
        SettingField(
            "smtp_host",
            "SMTP server",
            help_text="Such as smtp.example.test, from the service's SMTP settings.",
            validate=validate_host,
        ),
        SettingField("smtp_port", "Port", default="587", validate=validate_port),
        SettingField(
            "smtp_security",
            "Security",
            choices=(
                ("starttls", "STARTTLS (usually port 587)"),
                ("ssl", "SSL from the start (usually port 465)"),
            ),
            default="starttls",
        ),
        SettingField(
            "from_address",
            "Send from this address",
            help_text="The service must allow sending from it, usually by verifying the "
            "address or its domain.",
            validate=validate_address,
        ),
        SettingField(
            "from_name",
            "Sender name",
            required=False,
            help_text="Left blank, the firm's name from Firm settings.",
        ),
        SettingField(
            "reply_to",
            "Replies go to",
            required=False,
            help_text="Such as the office's main inbox, if replies should not go to the "
            "sending address.",
            validate=validate_address,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Email the CRM sends to clients, such as secure intake links and payment plan "
            "reminders: the client's email address, the subject, and the message",
        ),
        receives=("Only whether the server accepted each email",),
    )
    outbound_actions = {SEND: Capability.SYSTEM_EMAIL}

    def smtp(self):
        return open_smtp(
            self.setting("smtp_host"),
            self.setting("smtp_port") or 587,
            self.setting("smtp_security"),
            self.credentials.get("username", ""),
            self.credentials.get("password", ""),
            refused="The mail server did not accept the sign-in name and password.",
        )

    def test_connection(self):
        quit_quietly(self.smtp())

    def send(self, payload):
        sender = self.setting("from_address")
        domain = sender.rpartition("@")[2]
        message = EmailMessage()
        try:
            name = self.setting("from_name") or payload.get("sender_name", "")
            message["From"] = Address(" ".join(name.split()), addr_spec=sender)
            message["To"] = payload["to"]
            if self.setting("reply_to"):
                message["Reply-To"] = self.setting("reply_to")
            message["Subject"] = payload["subject"]
        except (ValueError, IndexError):
            raise ConnectorError("The email's address, sender, or subject was not valid.") from None
        message["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
        message["Message-ID"] = make_msgid(domain=domain or None)
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(payload["body"])
        server = self.smtp()
        try:
            server.send_message(message)
        except smtplib.SMTPRecipientsRefused:
            raise ConnectorError("The mail server refused the client's address.") from None
        except smtplib.SMTPSenderRefused:
            raise ConnectorError(
                "The mail server refused the sending address. Check that the service allows "
                "sending from it."
            ) from None
        except (OSError, smtplib.SMTPException):
            raise ConnectorError("The mail server did not take the email.") from None
        finally:
            quit_quietly(server)
        return message_key({"message-id": message["Message-ID"]})

    def perform(self, action, payload):
        if action != SEND:
            raise ConnectorError(f"The SMTP server cannot {action}.")
        return self.send(payload)
