"""IMAP and SMTP: each staff member's own mailbox, at almost any mail provider.

Each staff member connects their own mailbox with its address and an app password,
and the mail server names from their provider. Both connections always use TLS
(SSL, or STARTTLS for SMTP), with the server's certificate checked.

Mail: the first sync brings in the last 30 days of the inbox and the sent folder,
then each folder continues from the last message it saw. If the server renumbers a
folder (its UIDVALIDITY changes), the last 7 days are looked at again. Only headers
and a short plain-text excerpt are kept: the excerpt is read from messages under
256 KB, and attachments are never stored. Bulk and automated mail is skipped.
Messages are read without marking them as read.

Each message is filed under its Message-ID, which is also what sending returns, so
an email sent from the CRM and its copy in the sent folder land on one record.
Sending uses SMTP as plain text, through the outbound policy, and a copy is put in
the sent folder unless the provider keeps one itself.
"""

import imaplib
import re
import smtplib
import ssl
import time
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime

from django.core.exceptions import ValidationError
from django.utils.html import strip_tags

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
    SyncBatch,
)
from crm.connect.records import INBOUND, OUTBOUND, EmailRecord
from crm.connect.registry import register

BACKFILL_DAYS = 30
RESYNC_DAYS = 7
MAIL_LIMIT = 2000
# Messages up to this size are read in full for an excerpt; larger ones keep headers only.
SNIPPET_LIMIT = 256 * 1024
TIMEOUT = 30
SENT_NAMES = (
    "Sent",
    "Sent Items",
    "Sent Messages",
    "Sent Mail",
    "INBOX.Sent",
    "[Gmail]/Sent Mail",
)
HEADER_FIELDS = (
    "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES LIST-UNSUBSCRIBE PRECEDENCE "
    "AUTO-SUBMITTED"
)
LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
UID = re.compile(rb"UID (\d+)")


def validate_host(value):
    if not re.fullmatch(r"[A-Za-z0-9.-]+", value or "") or "." not in value:
        raise ValidationError("Enter the server's name, such as imap.example.test.")


def validate_port(value):
    if not str(value).isdigit() or not 0 < int(value) < 65536:
        raise ValidationError("Enter a port number, such as 993.")


def first_address(value):
    return next((address.lower() for _, address in getaddresses([value or ""]) if address), "")


def is_bulk(headers):
    """Mailing lists and automated mail, which are not client correspondence."""
    return (
        "list-unsubscribe" in headers
        or str(headers.get("precedence", "")).lower() in ("bulk", "list", "junk")
        or str(headers.get("auto-submitted", "no")).lower() != "no"
    )


def message_key(headers):
    """The Message-ID, without its brackets, lowercased; empty when there is none."""
    return str(headers.get("message-id") or "").strip().strip("<>").strip().lower()[:255]


def snippet_of(raw):
    """A short plain-text excerpt of a whole message."""
    message = message_from_bytes(raw, policy=policy.default)
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = body.get_content()
    except (LookupError, UnicodeError, ValueError):
        return ""
    if body.get_content_subtype() == "html":
        text = strip_tags(text)
    return re.sub(r"\s+", " ", text).strip()[:500]


def quoted(folder):
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    """The few IMAP commands the connector needs, with errors turned into ConnectorError."""

    def __init__(self, host, port, username, password):
        try:
            self.imap = imaplib.IMAP4_SSL(
                host, int(port), ssl_context=ssl.create_default_context(), timeout=TIMEOUT
            )
        except (OSError, ssl.SSLError, imaplib.IMAP4.error):
            raise ConnectorError(
                "The mail server could not be reached securely. Check the IMAP server and port."
            ) from None
        try:
            self.imap.login(username, password)
        except imaplib.IMAP4.error:
            self.close()
            raise ConnectorError(
                "The mail server did not accept the address and app password.", status=401
            ) from None

    def close(self):
        try:
            self.imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass

    def run(self, method, *args):
        try:
            status, data = getattr(self.imap, method)(*args)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ConnectorError(
                f"The mail server stopped answering ({exc.__class__.__name__})."
            ) from None
        if status != "OK":
            raise ConnectorError("The mail server refused a request.")
        return data

    def sent_folder(self, preferred=""):
        """The sent folder: the one the server marks as Sent, else a usual name."""
        if preferred:
            return preferred
        names = {}
        for line in self.run("list", '""', "*"):
            if not isinstance(line, bytes):
                continue
            match = LIST_LINE.match(line)
            if not match:
                continue
            name = match["name"].decode("utf-8", "replace").strip().strip('"')
            if b"\\sent" in match["flags"].lower():
                return name
            names[name.lower()] = name
        return next((names[n.lower()] for n in SENT_NAMES if n.lower() in names), "")

    def open(self, folder):
        """Select a folder read-only; return its UIDVALIDITY."""
        self.run("select", quoted(folder), True)
        _, values = self.imap.response("UIDVALIDITY")
        first = (values or [None])[0]
        return first.decode() if isinstance(first, bytes) else str(first or "")

    def search_since(self, day):
        return self.uids(self.run("uid", "SEARCH", None, "SINCE", day.strftime("%d-%b-%Y")))

    def search_after(self, last):
        # "UID n:*" always returns the highest message, even when it is older than n.
        found = self.uids(self.run("uid", "SEARCH", None, f"UID {last + 1}:*"))
        return [uid for uid in found if uid > last]

    @staticmethod
    def uids(data):
        words = b" ".join(d for d in data if isinstance(d, bytes)).split()
        return sorted({int(word) for word in words if word.isdigit()})

    def headers(self, uid):
        """(size, arrival time, raw headers) of one message, or None if it is gone."""
        items = f"(RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
        data = self.run("uid", "FETCH", str(uid), items)
        part = next((d for d in data if isinstance(d, tuple)), None)
        if part is None:
            return None
        size = SIZE.search(part[0])
        arrived = imaplib.Internaldate2tuple(part[0])
        return (
            int(size.group(1)) if size else 0,
            datetime.fromtimestamp(time.mktime(arrived), UTC) if arrived else None,
            part[1],
        )

    def whole(self, uid):
        data = self.run("uid", "FETCH", str(uid), "(BODY.PEEK[])")
        part = next((d for d in data if isinstance(d, tuple)), None)
        return part[1] if part else b""

    def append(self, folder, raw):
        self.run("append", quoted(folder), "\\Seen", imaplib.Time2Internaldate(time.time()), raw)


@register
class ImapSmtpConnector(Connector):
    key = "imap_smtp"
    name = "IMAP and SMTP email"
    description = (
        "Connect your own mailbox at almost any mail provider: email with clients is filed "
        "on the right contact, and you can send email from contact pages."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.MAILBOX})
    connection_scope = ConnectionScope.USER
    auth = ApiKeyAuth(
        fields=(
            CredentialField("email", "Email address"),
            CredentialField(
                "password",
                "App password",
                help_text="Create one in your mail provider's security settings for this CRM. "
                "Your usual password is not needed, and two-step sign-in can stay on.",
            ),
        ),
        label="Email and app password",
    )
    settings_fields = (
        SettingField(
            "imap_host",
            "IMAP server",
            help_text="From your mail provider, such as imap.example.test.",
            validate=validate_host,
        ),
        SettingField("imap_port", "IMAP port", default="993", validate=validate_port),
        SettingField(
            "smtp_host",
            "SMTP server",
            help_text="From your mail provider, such as smtp.example.test.",
            validate=validate_host,
        ),
        SettingField("smtp_port", "SMTP port", default="465", validate=validate_port),
        SettingField(
            "smtp_security",
            "SMTP security",
            choices=(
                ("ssl", "SSL from the start (usually port 465)"),
                ("starttls", "STARTTLS (usually port 587)"),
            ),
            default="ssl",
        ),
        SettingField(
            "username",
            "Sign-in name",
            required=False,
            help_text="Only if your provider's sign-in name is not your email address.",
        ),
        SettingField(
            "sent_folder",
            "Sent folder",
            required=False,
            help_text="Left blank, the folder your server marks as Sent is used.",
        ),
        SettingField(
            "unmatched",
            "Email from people who are not contacts",
            choices=(
                ("keep", "Keep it in Needs matching, to link or add as a contact"),
                ("skip", "Leave it out of the CRM"),
            ),
            default="keep",
            help_text="Email between staff members is always left out.",
        ),
        SettingField(
            "copy_to_sent",
            "Keep a copy of sent email",
            choices=(
                ("yes", "Put a copy in the Sent folder"),
                ("no", "No: my provider keeps one itself"),
            ),
            default="yes",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Email you send from contact and matter pages: the contact's address, the subject, "
            "and the message",
        ),
        receives=(
            "From your inbox and sent folder: the sender, recipients, subject, time, and a short "
            "excerpt of email with contacts, kept on the matching contact",
        ),
    )
    outbound_actions = {"send_email": Capability.MAILBOX}
    sync_capabilities = frozenset({Capability.MAILBOX})

    @property
    def address(self):
        return str(self.credentials.get("email", "")).strip().lower()

    @property
    def username(self):
        return self.setting("username") or self.address

    def mailbox(self):
        return Mailbox(
            self.setting("imap_host"),
            self.setting("imap_port") or "993",
            self.username,
            self.credentials.get("password", ""),
        )

    def smtp(self):
        host, port = self.setting("smtp_host"), int(self.setting("smtp_port") or 465)
        context = ssl.create_default_context()
        try:
            if self.setting("smtp_security") == "starttls":
                server = smtplib.SMTP(host, port, timeout=TIMEOUT)
                server.starttls(context=context)
            else:
                server = smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT)
        except (OSError, ssl.SSLError, smtplib.SMTPException):
            raise ConnectorError(
                "The outgoing mail server could not be reached securely. Check the SMTP server, "
                "port, and security."
            ) from None
        try:
            server.login(self.username, self.credentials.get("password", ""))
        except smtplib.SMTPAuthenticationError:
            server.close()
            raise ConnectorError(
                "The outgoing mail server did not accept the address and app password.",
                status=401,
            ) from None
        except (OSError, smtplib.SMTPException):
            server.close()
            raise ConnectorError("The outgoing mail server would not sign in.") from None
        return server

    def test_connection(self):
        mailbox = self.mailbox()
        try:
            mailbox.open("INBOX")
        finally:
            mailbox.close()
        server = self.smtp()
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):
            pass

    # Mail

    def sync(self, capability, cursor):
        cursor = dict(cursor or {})
        mailbox = self.mailbox()
        records, seen = [], set()
        try:
            folders = {"INBOX": INBOUND}
            sent = mailbox.sent_folder(self.setting("sent_folder"))
            if sent:
                folders[sent] = OUTBOUND
            for folder, direction in folders.items():
                state = cursor.get(folder) or {}
                validity = mailbox.open(folder)
                if state.get("validity") == validity and state.get("last"):
                    uids = mailbox.search_after(int(state["last"]))
                else:
                    days = RESYNC_DAYS if state else BACKFILL_DAYS
                    uids = mailbox.search_since(datetime.now(UTC) - timedelta(days=days))
                uids = uids[-MAIL_LIMIT:]
                for uid in uids:
                    record = self.email_record(mailbox, folder, validity, uid, direction)
                    if record and record.external_id not in seen:
                        seen.add(record.external_id)
                        records.append(record)
                last = max(uids) if uids else int(state.get("last") or 0)
                cursor[folder] = {"validity": validity, "last": last}
        finally:
            mailbox.close()
        return SyncBatch(records=records, cursor=cursor)

    def email_record(self, mailbox, folder, validity, uid, direction):
        fetched = mailbox.headers(uid)
        if fetched is None:
            return None
        size, arrived, raw = fetched
        message = message_from_bytes(raw, policy=policy.default)
        headers = {key.lower(): str(value) for key, value in message.items()}
        if is_bulk(headers):
            return None
        outbound = direction == OUTBOUND
        remote = first_address(headers.get("to" if outbound else "from"))
        if not remote or remote == self.address:
            return None
        try:
            sent_at = parsedate_to_datetime(headers.get("date", ""))
        except (TypeError, ValueError):
            sent_at = None
        occurred_at = sent_at if sent_at and sent_at.tzinfo else arrived
        if occurred_at is None:
            return None
        references = str(headers.get("references", "")).split()
        thread = (references[0] if references else headers.get("message-id", "")).strip("<> ")
        snippet = snippet_of(mailbox.whole(uid)) if 0 < size <= SNIPPET_LIMIT else ""
        return EmailRecord(
            external_id=message_key(headers) or f"{folder}:{validity}:{uid}"[:255],
            occurred_at=occurred_at,
            direction=direction,
            remote=remote,
            subject=" ".join(str(headers.get("subject", "")).split())[:255],
            snippet=snippet,
            thread_id=thread.lower()[:255],
            staff_email=self.address,
        )

    def send_email(self, payload):
        message = EmailMessage()
        try:
            message["From"] = self.address
            message["To"] = payload["to"]
            message["Subject"] = payload["subject"]
        except ValueError:
            raise ConnectorError("The email's address or subject was not valid.") from None
        message["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
        message["Message-ID"] = make_msgid(domain=self.address.rpartition("@")[2] or None)
        message.set_content(payload["body"])
        server = self.smtp()
        try:
            server.send_message(message)
        except (OSError, smtplib.SMTPException):
            raise ConnectorError("The outgoing mail server did not take the email.") from None
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                pass
        if self.setting("copy_to_sent") != "no":
            self.keep_copy(message.as_bytes())
        return message_key({"message-id": message["Message-ID"]})

    def keep_copy(self, raw):
        """Put a copy in the sent folder; the email is already sent, so a failure only loses it."""
        try:
            mailbox = self.mailbox()
        except ConnectorError:
            return
        try:
            sent = mailbox.sent_folder(self.setting("sent_folder"))
            if sent:
                mailbox.append(sent, raw)
        except ConnectorError:
            pass
        finally:
            mailbox.close()

    def perform(self, action, payload):
        if action != "send_email":
            raise ConnectorError(f"This mailbox cannot {action}.")
        return self.send_email(payload)
