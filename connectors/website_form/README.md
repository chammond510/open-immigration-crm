# Website form

Turns submissions from the firm's website form into new leads. A new person becomes a contact at the Inquiry stage, with the lead source filled in, and the submission is kept on the contact as an intake note. It is in Beta.

## What it does

- **New people.** A submission from someone the CRM does not know creates a contact at the Inquiry stage, as a lead. The connection can fill a contact choice field, such as the lead-source field the operating dashboard groups by, from the submission's `source` (or a `utm_` value). The value must match one of the field's choices, ignoring case, spaces, and punctuation; `google-ads` fills "Google Ads".
- **People it knows.** A submission from an existing contact, matched by email or phone, adds an intake note and changes nothing else.
- **The note.** The intake note has the message, the form's other answers, which form it came from, and its tracking values.
- **Signed only.** Every submission must be signed with the connection's secret. The CRM refuses one that is unsigned, wrongly signed, or more than five minutes old, and files each submission ID once, so a captured submission cannot be sent again.

## Setting it up

1. In the CRM, a superuser opens **Connections > Website form**. Copy the **signing secret** it shows; it is not shown again. Review what the connection receives, and connect.
2. On the connection's page, optionally choose the contact field to fill with the lead source. Then turn on webhooks and copy the address.
3. Give the address and the secret to whatever sends the submissions: the website's own server, a small serverless function, or a tool such as n8n or Make. A browser cannot sign a submission without revealing the secret, so the signing must happen there, not in the web page. The [automation recipes](../../docs/recipes/README.md) do this in n8n, Zapier, and Make.

## What to send

A `POST` with a JSON body, such as:

```json
{
  "name": "Lena Fictional",
  "email": "lena@example.test",
  "phone": "+1 202 555 0144",
  "message": "My spouse is a U.S. citizen. Can you help?",
  "form": "Contact page",
  "source": "google-ads",
  "utm_campaign": "fall",
  "answers": {"Preferred language": "Spanish"}
}
```

A name (`name`, or `first_name` and `last_name`) and an email or phone are required. Other plain values, at the top level or under `answers`, are kept as answers.

Sign it with the [Standard Webhooks](https://www.standardwebhooks.com) scheme, which most languages have a library for. By hand, it is three headers:

- `webhook-id`: a unique ID for this submission, such as a random UUID.
- `webhook-timestamp`: the time in whole seconds since 1970.
- `webhook-signature`: `v1,` followed by the base64 HMAC-SHA256 of `{webhook-id}.{webhook-timestamp}.{body}`, keyed with the base64-decoded part of the secret after `whsec_`.

In Python:

```python
import base64, hashlib, hmac, json, time, uuid
import urllib.request

def send(address, secret, submission):
    body = json.dumps(submission).encode()
    message_id, stamp = f"msg_{uuid.uuid4().hex}", str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{message_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    request = urllib.request.Request(address, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "webhook-id": message_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    })
    urllib.request.urlopen(request, timeout=10)
```

In Node.js:

```js
const crypto = require("node:crypto");

async function send(address, secret, submission) {
  const body = JSON.stringify(submission);
  const id = `msg_${crypto.randomUUID()}`;
  const stamp = Math.floor(Date.now() / 1000).toString();
  const key = Buffer.from(secret.replace(/^whsec_/, ""), "base64");
  const signature = crypto.createHmac("sha256", key).update(`${id}.${stamp}.${body}`).digest("base64");
  await fetch(address, {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      "webhook-id": id,
      "webhook-timestamp": stamp,
      "webhook-signature": `v1,${signature}`,
    },
  });
}
```

The CRM answers `200` when it accepted a submission, `401` when the signature or time is wrong, and `400` when the body is not a usable submission. It never says more.

Website forms attract spam. Filter it on the site, with a CAPTCHA or a hidden field, before sending submissions on.
