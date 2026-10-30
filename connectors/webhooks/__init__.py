"""Signed outbound webhooks to any address the firm chooses.

Deliveries follow the Standard Webhooks specification (standardwebhooks.com):
each POST carries ``webhook-id``, ``webhook-timestamp``, and ``webhook-signature``
headers, and the signature is HMAC-SHA256 over ``id.timestamp.body`` with the
connection's ``whsec_`` secret. n8n, Zapier, Make, Power Automate, and anything
else that accepts a webhook can receive them.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.registry import register

SECRET_PREFIX = "whsec_"
TIMEOUT_SECONDS = 10
USER_AGENT = "OpenImmigrationCRM-Webhooks/1"
LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def new_secret():
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()


def secret_key(secret):
    """The HMAC key: the base64 part of a whsec_ secret, or the raw text of any other."""
    if secret.startswith(SECRET_PREFIX):
        try:
            return base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
        except ValueError:
            pass
    return secret.encode()


def sign(secret, message_id, timestamp, body):
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def is_local_host(host):
    """Hosts on the firm's own network, such as a self-hosted n8n named in Docker Compose."""
    if host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def check_address(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and host and is_local_host(host):
        return
    raise ConnectorError(
        "The webhook address must start with https://. Plain http:// is allowed only "
        "for hosts on the firm's own network."
    )


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirects)


@register
class WebhooksConnector(Connector):
    key = "webhooks"
    name = "Webhooks"
    description = (
        "Send signed CRM events to any address you choose, such as an n8n, Zapier, Make, "
        "or Power Automate webhook."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AUTOMATION})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "url",
                "Webhook address",
                secret=False,
                help_text="The address your automation tool gives you, starting with https://.",
            ),
            CredentialField(
                "signing_secret",
                "Signing secret",
                generate=new_secret,
                help_text=(
                    "Copy this into the receiving tool if it checks signatures. "
                    "It is not shown again."
                ),
            ),
        ),
        docs_url="https://github.com/chammond510/open-immigration-crm/blob/main/docs/WEBHOOKS.md",
        label="Webhook address",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The events you choose, each with its type and time, the record's ID, and "
            "links to the record in the CRM and the API",
            "If you choose full records: each record as the API returns it, which can "
            "include client names, contact details, immigration details, intake answers, "
            "and billing amounts",
            "A test event with no client data when the connection is tested",
        ),
        receives=("Only whether each delivery was accepted",),
    )
    outbound_actions = {"deliver_event": Capability.AUTOMATION}

    def post(self, message_id, event):
        url = str(self.credentials.get("url", "")).strip()
        check_address(url)
        body = json.dumps(event, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "webhook-id": message_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": sign(
                    str(self.credentials.get("signing_secret", "")), message_id, timestamp, body
                ),
            },
        )
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ConnectorError(
                    f"The receiver answered HTTP {exc.code}, a redirect. "
                    "Enter the final address; deliveries do not follow redirects."
                ) from exc
            raise ConnectorError(f"The receiver answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectorError("The webhook address could not be reached.") from exc

    def test_connection(self):
        event = {
            "type": "webhook.test",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {"message": "A test event from Open Immigration CRM."},
        }
        self.post(f"msg_test_{uuid.uuid4().hex}", event)

    def perform(self, action, payload):
        if action != "deliver_event":
            raise ConnectorError(f"Webhooks cannot {action}.")
        status = self.post(f"msg_{payload['id']}", payload["event"])
        return f"HTTP {status}"
