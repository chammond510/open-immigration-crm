# Outbound webhooks

The **Webhooks** connector sends signed events to an address the firm chooses when something happens in the CRM, such as a new contact, a matter status change, or a logged USCIS notice. Tools like n8n, Zapier, Make, and Power Automate can react to them instead of polling the [API](API.md).

Deliveries follow the [Standard Webhooks](https://www.standardwebhooks.com/) specification, so its libraries for many languages can verify them.

## Set it up

1. **Get an address.** In the automation tool, create a webhook trigger and copy its address. Examples are n8n's Webhook node, Zapier's "Catch Hook", and Make's "Custom webhook".
2. **Connect.** In the CRM, open **Connections → Automation → Webhooks**.
   - Paste the address.
   - Copy the signing secret the form proposes into the tool, if the tool checks signatures. The secret is shown only on this form.
   - Review what the connection sends, then connect.
   - The CRM sends a `webhook.test` event to confirm the address works.
3. **Choose events.** On the connection's page, choose which events to send and whether each event carries IDs and links only, or the full record. Nothing is sent until events are chosen.
4. **Turn off preview mode.** New connections start in preview mode: events are recorded on the connection's page but not sent. Turn preview mode off when the receiver is ready.

The address must use `https://`. Plain `http://` is accepted only for hosts on the firm's own network, such as `http://n8n:5678/...` for an n8n container in the same Docker Compose project, `localhost`, private IP addresses, or names ending in `.local`, `.internal`, `.lan`, or `.home.arpa`.

To change the address or secret, disconnect and connect again.

## Events

| Event | When |
|---|---|
| `contact.created` | A contact is created |
| `contact.updated` | A contact is changed |
| `matter.created` | A matter is opened |
| `matter.updated` | A matter is changed |
| `matter.status_changed` | A matter's status changes; `changes` has the old and new status |
| `work_item.created` | A task, deadline, or appointment is added, including deadlines created from a notice |
| `work_item.completed` | A task, deadline, or appointment is completed |
| `note.created` | A note is added (not system activity) |
| `communication.logged` | A call, text, email, or meeting is logged, by staff, through the API, or by a connector |
| `notice.logged` | A USCIS notice is logged |
| `case_status.changed` | A receipt's USCIS status is recorded for the first time or changes |
| `intake.submitted` | A client submits an intake form |
| `invoice.issued` | An invoice is issued |
| `payment.recorded` | A payment is recorded |

Events are sent for changes made anywhere: the web pages, the API, connectors, and the administration pages.
- **Timing.** An event is queued only after the change is saved, so a change that fails is never announced.
- **One event per change.** Saving the same record more than once in one request sends one event. A new record's `.created` event is not followed by an `.updated` event for the same request.

## Payload

Each delivery is a POST with a JSON body:

```json
{
  "type": "matter.status_changed",
  "timestamp": "2026-03-02T15:04:05.123456+00:00",
  "data": {
    "object": "matter",
    "id": "8f2c…",
    "contact": "41d9…",
    "web_url": "https://crm.example.test/matters/8f2c…/",
    "api_url": "https://crm.example.test/api/v1/matters/8f2c…/",
    "changes": {"status": {"from": "pending", "to": "approved"}},
    "via": "crm"
  }
}
```

- **`object` and `id`.** These name the record. `contact`, `matter`, or `invoice` hold the IDs of related records where they apply.
- **`web_url` and `api_url`.** These link to the record in the CRM and in the API, where both exist. A receiver with an [API token](API.md) can fetch details from `api_url`.
- **`via`.** This is `api` when the change came through the API. `api_token` then holds the first characters of the token that made it. An automation that writes back through the API can use this to skip its own changes and avoid loops.
- **`record`.** This is present only when the connection is set to send the full record. It is the record as the API returns it; intake submissions and payments have their own shapes. Full records can include client names, contact details, immigration details, intake answers, and billing amounts. Choose this only for receivers the firm trusts with that information.
- **`timestamp`.** This is when the change happened. Deliveries can arrive out of order, especially after a retry, so use `timestamp` rather than arrival order.

## Verifying signatures

Every delivery carries three headers:

| Header | Value |
|---|---|
| `webhook-id` | The delivery's unique ID. It stays the same on retries, so a receiver can drop repeats. |
| `webhook-timestamp` | When this attempt was sent, in Unix seconds |
| `webhook-signature` | `v1,` followed by the base64 HMAC-SHA256 of `{webhook-id}.{webhook-timestamp}.{body}` |

The HMAC key is the signing secret without its `whsec_` prefix, base64-decoded. Check the signature against the raw body before parsing it. Reject timestamps more than five minutes old, and compare signatures in constant time.

In Python:

```python
import base64, hashlib, hmac, time

def verify(secret, headers, body):
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    fresh = abs(time.time() - int(headers["webhook-timestamp"])) < 300
    return fresh and any(
        hmac.compare_digest(expected, candidate)
        for candidate in headers["webhook-signature"].split()
    )
```

The [Standard Webhooks libraries](https://github.com/standard-webhooks/standard-webhooks) do the same check. Their `Webhook(secret).verify(body, headers)` accepts the secret as shown.

## Delivery, retries, and the record kept

- **Answering.** Answer with any 2xx status within 10 seconds. Redirects are not followed.
- **Retries.** A failed delivery is retried after 1 minute, 5 minutes, 30 minutes, 2 hours, 8 hours, and 24 hours. If it still fails, it is marked failed. A delivery held back by the connection's hourly limit is retried on the same schedule.
- **The outbound policy.** Deliveries run in the background worker (`python manage.py run_worker`) and pass the same outbound policy as every connector: the site-wide kill switch, preview mode, the reviewed data-flow description, and the hourly limit. A delivery the policy blocks for any other reason, such as a connection that needs attention, is recorded as blocked and is not retried.
- **What is kept.**
  - Each event is stored encrypted until it is sent, previewed, blocked, or given up on, and then the payload is deleted.
  - The connection's page lists recent deliveries and every attempt. The connection's last problem is shown when deliveries fail.

## Using the API alongside webhooks

Webhooks tell a tool that something changed; the API lets it read more and write back. A common pattern:
1. A `notice.logged` event arrives with IDs only.
2. The tool fetches `api_url` with a token that has `notices:read`.
3. The tool posts a note or task back through the API.
