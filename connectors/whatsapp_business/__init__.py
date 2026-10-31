"""WhatsApp Business: messages with clients through the firm's own WhatsApp Business Platform
number, on Meta's Cloud API.

The firm sets up its own Meta app with the WhatsApp product, its WhatsApp Business
Account, and a business phone number, and enters a system user's access token, the
number's and account's IDs, and the app secret. Staff send messages one at a time from
contact and matter pages through the outbound policy. WhatsApp allows free-form
messages only within 24 hours of the client's last message (its customer service
window), so outside it core offers the account's approved templates instead.

Incoming messages and delivery updates arrive by webhook, signed by Meta with the app
secret over the raw body (``X-Hub-Signature-256``), and may come several to a
delivery. Meta first checks the address with a GET carrying a verify token, which the
connector derives from the app secret and the address and shows when webhooks are
turned on. Messages for other numbers on the same app are ignored. A client who uses a
WhatsApp username may arrive without a phone number, as WhatsApp's user ID, and waits
in Needs matching.

When the worker handles a message with a photo, or a PDF or Word document, it fetches
the file from Meta, which keeps it for 7 days, and core keeps it with the message.
Meta's media links carry the access token, so a file is fetched only from Meta's own
hosts. Voice messages, videos, stickers, other kinds of file, and files over the
CRM's upload limit are noted on the message, not fetched.

Documentation (read 2026-10-31):
https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-management
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint
https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids
https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media
"""

import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from django.utils import timezone

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    MessageTemplate,
    TemplateField,
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient
from crm.connect.records import (
    ATTACHMENT_TYPES,
    INBOUND,
    OUTBOUND,
    Attachment,
    TextRecord,
    attachment_limit,
)
from crm.connect.registry import register

API_URL = "https://graph.facebook.com/v26.0"
SIGNATURE_HEADER = "X-Hub-Signature-256"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
PLACEHOLDER = re.compile(r"{{\s*([a-z0-9_]+)\s*}}")
MAX_BODY = 4096
TEMPLATE_LIMIT = 250
# Meta's error codes for a refused request, explained without the numbers involved.
REFUSALS = {
    0: "Meta did not accept the access token.",
    190: "Meta did not accept the access token. It may have expired or been revoked.",
    3: "The access token lacks a permission this needs.",
    10: "The access token lacks a permission this needs, or it was removed.",
    100: "Meta did not accept a value in the request.",
    368: "Meta has restricted the account for violating a platform policy.",
    4: "Meta's limit on requests from the app was reached. Try again later.",
    80007: "Meta's limit on requests for the account was reached. Try again later.",
    130429: "The firm's number reached WhatsApp's throughput limit. Try again shortly.",
    131026: "WhatsApp could not deliver the message. The number may not use WhatsApp, or "
    "the client may need to update the app or accept its terms.",
    131031: "Meta has locked or restricted the firm's WhatsApp account.",
    131042: "Meta reports a problem with the account's payment method.",
    131047: "More than 24 hours have passed since the client's last message, so WhatsApp "
    "allows only an approved template.",
    131048: "WhatsApp is limiting messages from the firm's number after spam reports.",
    131049: "WhatsApp held the message back to keep its users' experience healthy. Wait a "
    "day before trying again.",
    131050: "The client asked WhatsApp to stop marketing messages from the firm.",
    131051: "WhatsApp does not support that kind of message.",
    131056: "Too many messages went to this client in a short time. Wait a little.",
    131057: "The firm's WhatsApp account is in maintenance. Try again shortly.",
    132000: "The template was sent with the wrong number of values.",
    132001: "WhatsApp has no approved template by that name and language.",
    132005: "The template's text is too long with these values.",
    132007: "The template breaks WhatsApp's policies.",
    132012: "A template value is in the wrong format.",
    132015: "The template is paused because of low quality ratings.",
    132016: "The template is disabled because of low quality ratings.",
    132018: "WhatsApp did not accept the template's values.",
    133010: "The firm's number is not registered on the WhatsApp Business Platform.",
}
PERMISSION_CODES = range(200, 300)
# What an incoming attachment is called in its note.
ATTACHMENTS = {
    "image": "a photo",
    "document": "a document",
    "audio": "a voice message or audio file",
    "video": "a video",
    "sticker": "a sticker",
}
# The kinds of attachment fetched, if core keeps their type, and the extensions a kept
# file's name may have.
FETCHED = {"image", "document"}
EXTENSIONS = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "application/pdf": (".pdf",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
}
# Meta serves media from its own hosts; the access token is sent to no others.
MEDIA_HOSTS = ("fbsbx.com", "facebook.com", "fbcdn.net", "whatsapp.net")
# WhatsApp's delivery statuses that core ranks; others, such as played, are left out.
STATUSES = {"sent", "delivered", "read", "failed"}


def to_e164(wa_id):
    """A WhatsApp ID, which is the number without its +, as an E.164 number."""
    number = "+" + str(wa_id or "").strip().lstrip("+")
    return number if E164.match(number) else ""


def message_time(value):
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def field_label(name, kind):
    """A field's label: a named value's name, or a numbered value's place."""
    where = "header" if kind == "header" else "message"
    if name.isdigit():
        return f"Value {name} in the {where}"
    label = name.replace("_", " ").capitalize()
    return f"{label} (header)" if kind == "header" else label


def describe(message):
    """The words of an incoming message, or a note of what it was.

    "" for a reaction, and for an attachment without a caption, whose note depends on
    whether the file is fetched.
    """
    kind = message.get("type", "")
    if kind == "text":
        return str((message.get("text") or {}).get("body") or "")
    if kind == "button":
        return str((message.get("button") or {}).get("text") or "")
    if kind == "interactive":
        reply = message.get("interactive") or {}
        chosen = reply.get("button_reply") or reply.get("list_reply") or {}
        return str(chosen.get("title") or "")
    if kind in ATTACHMENTS:
        return str((message.get(kind) or {}).get("caption") or "").strip()
    if kind == "location":
        place = message.get("location") or {}
        named = ", ".join(str(place[k]) for k in ("name", "address") if place.get(k))
        return f"[Shared a location{': ' + named if named else ''}.]"
    if kind == "contacts":
        return "[Shared a contact card.]"
    if kind == "system":
        return f"[WhatsApp: {(message.get('system') or {}).get('body') or 'an account change'}]"
    if kind == "reaction":
        return ""
    return "[Sent a kind of message WhatsApp does not share with the CRM.]"


@register
class WhatsAppBusinessConnector(Connector):
    key = "whatsapp_business"
    name = "WhatsApp Business"
    description = (
        "Message clients on WhatsApp from contact and matter pages through the firm's own "
        "WhatsApp Business Platform number, with approved templates outside WhatsApp's "
        "24-hour window, and file their replies on the right contact."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TEXTING})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "access_token",
                "Access token",
                help_text="A system user's token with the whatsapp_business_messaging and "
                "whatsapp_business_management permissions.",
            ),
            CredentialField(
                "phone_number_id",
                "Phone number ID",
                secret=False,
                help_text="On the app's WhatsApp API Setup page, under the firm's number.",
            ),
            CredentialField(
                "business_account_id",
                "WhatsApp Business Account ID",
                secret=False,
                help_text="On the same page. Templates are listed from this account.",
            ),
            CredentialField(
                "app_secret",
                "App secret",
                help_text="In the app's settings, under Basic. Meta signs the events it sends "
                "this CRM with it.",
            ),
        ),
        docs_url="https://developers.facebook.com/documentation/business-messaging/whatsapp/"
        "get-started",
        label="WhatsApp Cloud API credentials",
    )
    data_flows = DataFlows(
        version=2,
        sends=(
            "Messages staff send: the client's phone number and the message, or the chosen "
            "template and the values staff fill in",
        ),
        receives=(
            "Messages clients send to the firm's number: their number, or their WhatsApp "
            "user ID when WhatsApp leaves the number out, and the words",
            "Photos, and PDF or Word documents, clients send with a message, fetched from "
            "Meta and kept with it; other attachments are only noted",
            "Delivery and read updates for messages sent from this CRM",
            "The account's approved message templates",
        ),
    )
    outbound_actions = {"send_text": Capability.TEXTING, "send_template": Capability.TEXTING}
    reply_window = timedelta(hours=24)
    sharing_limits = (
        "Meta's terms for the WhatsApp Business Platform limit sharing WhatsApp data with "
        "third parties, which may include AI and translation services."
    )
    webhooks = WebhookSpec(
        capabilities=(Capability.TEXTING,),
        secret_source="credential",
        secret_credential="app_secret",
        instructions="In the Meta app's WhatsApp Configuration page, enter this address as "
        "the callback URL and the verify token above, choose Verify and save, then subscribe "
        "to the messages field.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="Meta",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('access_token', '')}",
            },
        )

    def account_part(self, name, label):
        value = str(self.credentials.get(name, "")).strip()
        if not value.isdigit():
            raise ConnectorError(f"The {label} should be digits only.")
        return value

    @property
    def number_id(self):
        return self.account_part("phone_number_id", "phone number ID")

    @property
    def account_id(self):
        return self.account_part("business_account_id", "WhatsApp Business Account ID")

    def call(self, method, path, **kwargs):
        """A Graph API answer, with Meta's refusals explained by error code."""
        response = self.client.request(method, path, expected=(200, 400, 401, 403, 404), **kwargs)
        try:
            answer = response.json() if response.content else {}
        except ValueError:
            answer = {}
        if response.status_code < 400:
            if not isinstance(answer, dict):
                raise ConnectorError("Meta sent a response that was not understood.")
            return answer
        error = answer.get("error") if isinstance(answer, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code in PERMISSION_CODES:
            code = 10
        message = REFUSALS.get(code) or (
            f"Meta refused the request (error {code})."
            if code
            else f"Meta answered HTTP {response.status_code}."
        )
        raise ConnectorError(message, status=response.status_code)

    def test_connection(self):
        number = self.call(
            "GET",
            f"/{self.number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
        )
        if not number.get("display_phone_number"):
            raise ConnectorError("Meta did not find the firm's WhatsApp number.")
        self.call(
            "GET", f"/{self.account_id}/message_templates", params={"limit": 1, "fields": "name"}
        )

    # Sending

    def send(self, to, message):
        if not E164.match(str(to or "")):
            raise ConnectorError("The client's number is not a valid international number.")
        answer = self.call(
            "POST",
            f"/{self.number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                **message,
            },
        )
        sent = (answer.get("messages") or [{}])[0]
        if not isinstance(sent, dict) or not sent.get("id"):
            raise ConnectorError("Meta did not return a message ID.")
        return str(sent["id"])

    def perform(self, action, payload):
        if action == "send_text":
            body = str(payload.get("body", ""))
            if len(body) > MAX_BODY:
                raise ConnectorError(f"WhatsApp messages can be at most {MAX_BODY} characters.")
            return self.send(
                payload["to"], {"type": "text", "text": {"preview_url": False, "body": body}}
            )
        if action == "send_template":
            return self.send(payload["to"], template_message(payload))
        raise ConnectorError(f"WhatsApp Business cannot {action}.")

    # Templates

    def message_templates(self):
        """The account's approved templates that need only text values, by name."""
        found = self.client.paginate(
            f"/{self.account_id}/message_templates",
            params={
                "status": "APPROVED",
                "fields": "name,language,status,category,components",
                "limit": 100,
            },
            items=lambda page: page.get("data") or [],
            next_request=lambda page: (
                (page["paging"]["next"], None) if (page.get("paging") or {}).get("next") else None
            ),
            limit=TEMPLATE_LIMIT,
        )
        templates = [template for item in found if (template := template_of(item))]
        return sorted(templates, key=lambda t: (t.name, t.language))

    # Webhooks

    def verify_token(self, url):
        """The token Meta must send back to check this address: from the secret and the path."""
        secret = str(self.credentials.get("app_secret", "")).encode()
        path = urlsplit(url).path.encode()
        return hmac.new(secret, b"verify-token:" + path, hashlib.sha256).hexdigest()[:40]

    def webhook_details(self, url):
        return (("Verify token", self.verify_token(url)),)

    def webhook_challenge(self, url, body, headers):
        query = parse_qs(urlsplit(url).query)
        if "hub.mode" not in query and "hub.challenge" not in query:
            return None
        challenge = (query.get("hub.challenge") or [""])[0]
        given = (query.get("hub.verify_token") or [""])[0]
        if (
            (query.get("hub.mode") or [""])[0] != "subscribe"
            or body
            or not challenge.isdigit()
            or len(challenge) > 64
            or not hmac.compare_digest(given.encode(), self.verify_token(url).encode())
        ):
            return 403, "forbidden"
        return 200, challenge

    def verify_webhook(self, body, headers, secret, *, url=""):
        given = headers.get(SIGNATURE_HEADER, "")
        if not given.startswith("sha256=") or not secret:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given.removeprefix("sha256=").lower(), expected)

    def parse_webhook(self, body, headers):
        try:
            data = json.loads(body)
        except ValueError:
            raise ConnectorError("The WhatsApp event was not JSON.") from None
        if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
            raise ConnectorError("The event was not from a WhatsApp Business Account.")
        number = str(self.credentials.get("phone_number_id", ""))
        items = []
        for entry in data.get("entry") or []:
            changes = entry.get("changes") if isinstance(entry, dict) else None
            for change in changes or []:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                if str((value.get("metadata") or {}).get("phone_number_id")) != number:
                    continue  # Another number on the same app.
                senders = {
                    str(c.get("user_id") or c.get("wa_id") or ""): c
                    for c in value.get("contacts") or []
                    if isinstance(c, dict)
                }
                items += [incoming(m, senders) for m in value.get("messages") or []]
                items += [delivery_update(s) for s in value.get("statuses") or []]
        # Meta resends the same body when it retries a delivery.
        return WebhookEnvelope(
            event_id=hashlib.sha256(body).hexdigest(),
            capability=Capability.TEXTING,
            payload={"items": [item for item in items if item]},
        )

    def handle_webhook(self, capability, payload):
        records = []
        for item in payload.get("items") or []:
            sent_at = datetime.fromisoformat(item["at"])
            body, attachments = item["body"], ()
            if item.get("media"):
                attachment, note = self.fetch_media(item["media"], sent_at)
                if attachment:
                    # A kept file needs no note when its caption says what it is.
                    attachments, note = (attachment,), "" if body else note
                body = "\n\n".join(part for part in (body, note) if part)
            records.append(
                TextRecord(
                    external_id=item["id"],
                    occurred_at=sent_at,
                    direction=item["direction"],
                    remote=item["remote"],
                    body=body,
                    status=item["status"],
                    attachments=attachments,
                )
            )
        return records

    # Attachments

    def fetch_media(self, media, sent_at):
        """A file a client sent, as an Attachment, and a note for staff.

        The note says what was sent when there is no caption, or why the file was
        not fetched.
        """
        what = ATTACHMENTS[media["kind"]]
        mime_type = str(media.get("mime_type") or "").split(";")[0].strip().lower()
        if media["kind"] not in FETCHED or mime_type not in ATTACHMENT_TYPES:
            return None, (
                f"[Sent {what}, which the CRM does not keep. Ask the client to send it "
                "another way if it is needed.]"
            )
        limit = attachment_limit()
        too_large = (
            f"[Sent {what} larger than the CRM keeps ({limit // (1024 * 1024)} MB). Ask the "
            "client to send it another way if it is needed.]"
        )
        try:
            if not str(media.get("id") or "").isdigit():
                raise ConnectorError("The media ID was not understood.")
            info = self.call("GET", f"/{media['id']}", params={"phone_number_id": self.number_id})
            if int(info.get("file_size") or 0) > limit:
                return None, too_large
            url = str(info.get("url") or "")
            host = urlsplit(url).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS):
                raise ConnectorError("Meta gave a media address outside its own hosts.")
            media_client = ProviderClient(
                name="Meta",
                headers={"Authorization": self.client.headers["Authorization"]},
                max_bytes=limit,
            )
            content = media_client.request("GET", url).content
        except (ConnectorError, TypeError, ValueError):
            return None, (
                f"[Sent {what}, which could not be fetched from WhatsApp. Ask the client to "
                "send it again if it is needed.]"
            )
        name = os.path.basename(str(media.get("filename") or "")).strip()
        if not name:
            label = "photo" if media["kind"] == "image" else "document"
            name = f"whatsapp-{label}-{timezone.localtime(sent_at):%Y%m%d-%H%M%S}"
        if not name.lower().endswith(EXTENSIONS[mime_type]):
            name += EXTENSIONS[mime_type][0]
        return Attachment(filename=name, mime_type=mime_type, content=content), f"[Sent {what}.]"


def template_message(payload):
    """The ``template`` message for a template id (name:language) and its field values.

    Field keys are ``header_`` or ``body_`` and the value's name: digits for numbered
    values, sent in order, or a name, sent as the parameter's name.
    """
    name, _, language = str(payload.get("template", "")).partition(":")
    if not name or not language:
        raise ConnectorError("Choose an approved template.")
    components = []
    for kind in ("header", "body"):
        values = {
            key.removeprefix(f"{kind}_"): str(value)
            for key, value in (payload.get("values") or {}).items()
            if key.startswith(f"{kind}_")
        }
        if not values:
            continue
        if all(key.isdigit() for key in values):
            parameters = [{"type": "text", "text": values[k]} for k in sorted(values, key=int)]
        else:
            parameters = [
                {"type": "text", "parameter_name": k, "text": v} for k, v in values.items()
            ]
        components.append({"type": kind, "parameters": parameters})
    template = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    return {"type": "template", "template": template}


def template_of(item):
    """A MessageTemplate for one of Meta's templates, or None when it needs more than text.

    Templates with a photo, video, document, or location header, a link button with a
    value, or any other button that needs one cannot be filled in here, and
    authentication templates carry one-time codes, so those are left out.
    """
    if not isinstance(item, dict) or item.get("status") != "APPROVED":
        return None
    if item.get("category") == "AUTHENTICATION":
        return None
    name, language = str(item.get("name") or ""), str(item.get("language") or "")
    if not name or not language:
        return None
    parts, fields = [], []
    for component in item.get("components") or []:
        kind = str(component.get("type") or "").upper()
        if kind == "HEADER":
            if str(component.get("format") or "TEXT").upper() != "TEXT":
                return None
            parts.append(marked(component, "header", fields))
        elif kind == "BODY":
            parts.append(marked(component, "body", fields))
        elif kind == "FOOTER":
            parts.append(str(component.get("text") or ""))
        elif kind == "BUTTONS":
            for button in component.get("buttons") or []:
                plain = button.get("type") in ("QUICK_REPLY", "PHONE_NUMBER") or (
                    button.get("type") == "URL" and not PLACEHOLDER.search(button.get("url", ""))
                )
                if not plain:
                    return None
                parts.append(f"[{button.get('text', '')}]")
        else:
            return None
    return MessageTemplate(
        id=f"{name}:{language}",
        name=name,
        text="\n\n".join(part for part in parts if part),
        fields=tuple(fields),
        language=language,
        category=str(item.get("category") or "").lower(),
    )


def marked(component, kind, fields):
    """A header's or body's text with each value's place marked by its field key."""
    example = component.get("example") or {}
    numbered = example.get(f"{kind}_text") or []
    if numbered and isinstance(numbered[0], list):
        numbered = numbered[0]  # The body's examples come as a list of lists.
    named = {
        str(item.get("param_name")): str(item.get("example") or "")
        for item in example.get(f"{kind}_text_named_params") or []
        if isinstance(item, dict)
    }

    def mark(match):
        name = match.group(1)
        key = f"{kind}_{name}"
        if all(f.key != key for f in fields):
            if name.isdigit():
                place = int(name) - 1
                sample = str(numbered[place]) if 0 <= place < len(numbered) else ""
            else:
                sample = named.get(name, "")
            fields.append(TemplateField(key, field_label(name, kind), sample))
        return "{{" + key + "}}"

    return PLACEHOLDER.sub(mark, str(component.get("text") or ""))


def incoming(message, senders):
    """An incoming message, or None for one with nothing to file, such as a reaction."""
    if not isinstance(message, dict):
        return None
    sent_at = message_time(message.get("timestamp"))
    remote = to_e164(message.get("from"))
    if not remote and message.get("from_user_id"):
        # A client with a WhatsApp username, whose number WhatsApp left out.
        user_id = str(message["from_user_id"])
        profile = (senders.get(user_id) or {}).get("profile") or {}
        # Without an @, so it is never read as an email address.
        name = str(profile.get("username") or user_id).replace("@", "")
        remote = f"WhatsApp user {name}"
    body = describe(message)
    kind = message.get("type", "")
    item = {
        "id": str(message.get("id") or "")[:255],
        "direction": INBOUND,
        "remote": remote[:255],
        "body": body[:MAX_BODY],
        "status": "received",
        "at": sent_at.isoformat() if sent_at else "",
    }
    if kind in ATTACHMENTS:
        # Fetched by the worker, which handles the stored event.
        media = message.get(kind) if isinstance(message.get(kind), dict) else {}
        item["media"] = {
            "kind": kind,
            "id": str(media.get("id") or "")[:64],
            "mime_type": str(media.get("mime_type") or "")[:120],
            "filename": str(media.get("filename") or "")[:255],
        }
    elif not body:
        return None
    return item if item["id"] and sent_at and remote else None


def delivery_update(status):
    """A delivery update for a sent message; its number may be left out for usernames."""
    if not isinstance(status, dict):
        return None
    at = message_time(status.get("timestamp"))
    if not (status.get("id") and at and status.get("status") in STATUSES):
        return None
    return {
        "id": str(status["id"])[:255],
        "direction": OUTBOUND,
        "remote": to_e164(status.get("recipient_id")),
        "body": "",
        "status": status["status"],
        "at": at.isoformat(),
    }
