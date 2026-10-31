import hashlib

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .base import ConnectorError
from .models import Connection, WebhookEvent
from .tasks import process_webhook_event

ACCEPTING_STATUSES = (Connection.Status.ACTIVE, Connection.Status.NEEDS_ATTENTION)


def plain(status, text):
    return HttpResponse(text, status=status, content_type="text/plain")


@csrf_exempt
@require_http_methods(["GET", "POST"])
def receive_webhook(request, token):
    """Accept a provider event: verify, store minimized and encrypted, and queue it.

    Every failure answers without detail, and nothing is stored unless the
    signature verifies. Providers retry, so a repeated event is acknowledged
    without being queued again. A GET is only ever a provider checking the address,
    which the connector's ``webhook_challenge()`` answers.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection = Connection.objects.filter(
        webhook_token_hash=token_hash, status__in=ACCEPTING_STATUSES
    ).first()
    connector = connection.connector() if connection else None
    if connector is None or connector.webhooks is None:
        return plain(404, "not found")
    body = request.body
    if len(body) > settings.CONNECTOR_WEBHOOK_MAX_BYTES:
        return plain(413, "too large")
    url = request.build_absolute_uri()
    challenge = connector.webhook_challenge(url, body, request.headers)
    if challenge is not None:
        return plain(*challenge)
    if request.method != "POST":
        return plain(405, "method not allowed")
    secret = connection.webhook_secret or ""
    if not secret or not connector.verify_webhook(body, request.headers, secret, url=url):
        return plain(401, "invalid signature")
    try:
        envelope = connector.parse_webhook(body, request.headers)
    except ConnectorError:
        return plain(400, "malformed event")
    if not envelope.event_id or envelope.capability not in connector.webhooks.capabilities:
        return plain(400, "malformed event")
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                connection=connection,
                event_id=envelope.event_id[:255],
                capability=envelope.capability,
                payload=envelope.payload,
            )
            transaction.on_commit(lambda: process_webhook_event.enqueue(event.pk))
    except IntegrityError:
        # A repeat delivery; it gets the same answer as the first.
        pass
    spec = connector.webhooks
    return HttpResponse(spec.ack_body, status=spec.ack_status, content_type=spec.ack_content_type)
