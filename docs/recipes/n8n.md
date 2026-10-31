# n8n recipes

Two workflows to import into n8n, for n8n Cloud or a self-hosted n8n, such as one in the CRM's own Docker Compose project. See [the recipes overview](README.md) for what they do and where data goes.

## Before you import

The workflows' Code nodes use Node's `crypto` module.
- **n8n Cloud** allows it.
- **Self-hosted n8n** blocks it unless it is allowed:
  - With n8n's default task runner, start n8n with `NODE_FUNCTION_ALLOW_BUILTIN=crypto`.
  - The separate `n8nio/runners` image allows `crypto` already.

Import a workflow from the workflow menu with **Import from File**, or paste the file's contents onto the canvas.

## Task when a USCIS status changes

[`n8n/case-status-task.json`](n8n/case-status-task.json):

1. **CRM event** receives each event with its raw body.
2. **Check signature** checks it. **Signed?** answers a bad or old event with `401` (**Refuse**) and a good one with `200` (**Accept**).
3. **Status change?** passes on only `case_status.changed` events.
4. **Add a task** posts a high-priority task on the event's matter to `/api/v1/work-items/`.

Set it up:

1. **In the CRM**, create an [API token](../API.md#tokens) with only the `work:write` scope.
2. **In n8n**, open **Add a task**.
   - Replace `crm.example.test` with the CRM's address.
   - Under **Bearer Auth**, create a credential with the token.
3. **In the CRM**, open **Connections → Automation → Webhooks** and leave the form open. Copy the signing secret it shows into **Check signature**, in place of `PASTE_THE_SIGNING_SECRET`.
4. **Publish the workflow** (on n8n 1.x, activate it). Then copy the production URL from **CRM event**. It ends in `/webhook/crm-events`. The test URL works only while the editor is listening.
5. **In the CRM**, paste that URL into the form and connect. Connecting sends a `webhook.test` event, which should show as a successful run in n8n.
6. **Choose the event.** On the connection's page, choose the `case_status.changed` event with IDs and links only. Then turn preview mode off.

## Website form leads

[`n8n/website-form-lead.json`](n8n/website-form-lead.json):

1. **Website form** receives a submission from the website, protected with a header that only the website knows.
2. **Sign for the CRM** maps the form's fields and signs the submission.
3. **Send to the CRM** posts it to the Website form connection's address.

The workflow keeps no copy of runs that succeed, because submissions carry client details. Runs that fail are kept, so they can be fixed.

Set it up:

1. **In the CRM**, a superuser opens **Connections → Website form**, copies the signing secret, and connects. On the connection's page, turn on webhooks and copy the address.
2. **In n8n,** set up the three nodes:
   - On **Website form**, create a **Header Auth** credential with a header name, such as `X-Form-Key`, and a long random value.
   - In **Sign for the CRM**, paste the secret in place of `PASTE_THE_SIGNING_SECRET`. Change the field names on the right of `submission` to the names the form sends.
   - In **Send to the CRM**, replace the address with the connection's address.
3. **Publish the workflow** and copy the production URL from **Website form**.
4. **On the website**, have the form's webhook add-on post each submission to that URL as JSON, with the header from step 2. Examples of such add-ons are the Gravity Forms and WPForms webhook add-ons.

Filter spam on the website, with a CAPTCHA or a hidden field, before it reaches n8n.

Send `payload` exactly as **Sign for the CRM** made it: the CRM checks the signature against those bytes. If the CRM answers `401`, check the secret and that the body was not changed on the way.
