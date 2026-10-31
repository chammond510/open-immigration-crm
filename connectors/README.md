# Connectors

Each subpackage here is one optional provider adapter. Core code never imports these packages; `crm/connect/registry.py` loads them at startup, and installed third-party packages can add more through the `open_immigration_crm.connectors` entry point. The design is in [docs/CONNECTOR_PLAN.md](../docs/CONNECTOR_PLAN.md).

## Layout

```text
connectors/
  catalog.toml          wanted providers shown in the catalog before a connector exists
  anthropic/            Claude models through a Claude Console API key
  calendly/             consultations booked through Calendly, by signed organization webhook
  dialpad/              Dialpad calls and texts, by signed webhook, with a call backfill
  google_workspace/     each staff member's Gmail and Google Calendar
  lawpay/               LawPay payment links per account, and payments by polling
  microsoft_365/        each staff member's Outlook mail and calendar, through Microsoft Graph
  openai/               OpenAI models through the Responses API, with store: false
  openai_compatible/    any OpenAI-compatible server, including local models such as Ollama
  openrouter/           many models through OpenRouter, connected by signing in
  example/              a fictional connector that tests the framework (hidden from the catalog)
  example_ai/           a fictional AI model for testing the AI interface (hidden)
    __init__.py         the connector class, registered with @register
    test_example.py     the shared contract tests plus connector-specific tests
  twilio/               Twilio texts, sent through the outbound policy and received by webhook
  uscis_case_status/    USCIS Case Status API, through the firm's own developer.uscis.gov app
  webhooks/             the built-in automation connector: signed events to any address
```

## Writing a connector

To build a connector as a package of its own, outside this repository, start with [docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md) and `python manage.py startconnector`; the rest of this section applies to it too.

1. Subclass `crm.connect.base.Connector` and declare the manifest as class attributes: `key`, `name`, `description`, `maturity`, `capabilities`, `auth`, `connection_scope`, `data_flows`, `maintainers`, `outbound_actions`, and `settings_fields`. Register the class with `@register` from `crm.connect.registry`. A manifest that is incomplete is rejected at startup.
2. Implement `test_connection()`, plus:
   - `sync()` for the capabilities listed in `sync_capabilities` (the worker runs it every `sync_interval`);
   - `perform()` for outbound actions;
   - for providers that push events, a `webhooks` spec plus `verify_webhook()` (constant-time signature check; never accept unsigned requests), `parse_webhook()` (keep only what processing needs), and `handle_webhook()`.

   Webhook options in `WebhookSpec`:
   - `secret_source`: `"crm"` (the CRM generates the signing secret), `"provider"` (staff paste the provider's), or `"credential"` (the provider signs with a credential already entered, named by `secret_credential`, such as Twilio's Auth Token).
   - `verify_webhook()` also receives `url`, the address the request came to, for providers that sign it.
   - `register_label` offers, when staff turn webhooks on, to set the address at the provider through `register_webhook(url, secret)`. Say plainly what it changes there.
   - `ack_status`, `ack_body`, and `ack_content_type` are the answer to an accepted event, for providers that act on it. Twilio, for one, would text a plain-text answer back to the sender, and Microsoft Graph expects 202.
   - `webhook_challenge(url, body, headers)` answers a provider's check that the address is ours, such as Microsoft Graph's `validationToken`, before any verification. Echo only what the provider sent, and act on nothing in it.

   Texting connectors declare a `send_text` outbound action. Its payload is `{"to": E.164 number, "body": text, "staff_email": the sender's email}`, and it returns the provider's message ID. Core's "Send a text" form uses it through the outbound policy and logs the sent text under that ID, so the provider's own copy and later delivery updates (`TextRecord.status`) land on the same communication. A `TextRecord` can carry `opt_out` (`stop` or `start`) when the provider recognized an opt-out reply itself.

   Mailbox connectors declare a `send_email` outbound action. Its payload is `{"to": address, "subject": text, "body": plain text, "staff_email": the sender's email}`, and it returns the provider's message ID, which should be the ID the mailbox's own copy syncs under. Core's "Send an email" form uses it like "Send a text".

   System email connectors declare `send_system_email`, for the email the CRM sends itself, such as secure intake links and payment plan reminders. Its payload is `{"to": address, "subject": text, "body": plain text, "sender_name": the firm's name}`, and it returns the message ID. Send it from the firm's address, as plain text, marked `Auto-Submitted: auto-generated`. The firm's oldest active system email connection sends, and core logs each sent email on the contact with its own summary, never the body.

   E-signature connectors declare `send_for_signature` and `cancel_signature`. The `send_for_signature` payload is `{"title", "filename", "pdf" (base64), "signers": [{"name", "email"}], "message"}`; the provider emails each signer, and the action returns `{"id"}`, the provider's ID for the document. `cancel_signature` takes `{"document_id"}`. Report progress as `SignatureRecord`s under that ID, with `status` `signed` (one signer, named by `signer_email`), `completed` (with the signed PDF's bytes in `signed_pdf`), `declined`, or `cancelled`. Core ignores reports for documents it did not send.

   Document storage connectors declare `save_folder`, `save_file`, and `trash_item`, for a firm-wide connection. `save_folder` takes `{"folder_id": the provider's ID or "", "name", "crm_id"}`, makes the matter's folder inside the folder the firm chose in the connection's settings, or renames it, and returns `{"id"}`; make a new one if `folder_id` no longer exists. `save_file` takes `{"folder_id", "name", "mime_type", "content" (base64), "crm_id"}` and returns `{"id"}`. `trash_item` takes `{"item_id"}`, moves it to the trash rather than deleting it, and treats one already gone as done. Core never reads files back.

   Payment connectors can declare `create_payment_link`. Its payload is `{"invoice_number", "amount" (a decimal string), "currency", "deposit_account" ("operating" or "trust"), "email", "name"}`, and it returns the link's HTTPS address. Core asks for one link per account still owed on an invoice, because advances go to trust and earned fees to operating, never mixed. The payment should come back as a `PaymentRecord` with the invoice number as `reference` and the account as `deposit_account`. A `PaymentRecord` with status `refunded` voids the payment, and `partially_refunded` leaves it for staff to adjust.

   Calendar connectors that take the CRM's appointments declare `save_event` and `delete_event`. The `save_event` payload is `{"event_id": the provider's ID or "", "title", "starts_at", "ends_at" (ISO 8601), "time_zone", "description", "crm_id"}`, and it returns the event's ID; create the event when `event_id` is empty or no longer exists. Add no attendees and send no invitations. Core puts each appointment on its staff member's connected calendars and keeps it in step, and it only changes events it created (`ExternalRef.created_by_crm`). List both actions in `owner_sync_actions`, which lets them run automatically on a personal connection, but only for records assigned to its owner.

   Video connectors declare `save_meeting` and `delete_meeting`, both in `owner_sync_actions`. The `save_meeting` payload is `{"meeting_id": the provider's ID or "", "title", "starts_at" (ISO 8601), "duration_minutes", "time_zone", "crm_id"}`, and it returns `{"id", "join_url"}` (an `https://` link); make a new meeting when `meeting_id` is empty or no longer exists. `delete_meeting` takes `{"meeting_id"}` and treats a meeting already gone as deleted. Return only what a guest joins with, never a host's start link, and send no invitations.

   A connector can declare a setting named `unmatched` with the choices `keep` and `skip`. With `skip`, core stores only records whose other party is a known contact, instead of sending the rest to Needs matching. Email between staff members is never stored.

   AI model connectors declare `generate`. Core's `crm/ai/client.py` describes the payload (system prompt, messages, function tools, and an optional JSON schema) and the reply. Return `raw`, the provider's own copy of the assistant's turn, so core can send it back unchanged in a tool loop. Reasoning that must be passed back exactly needs this.

   Staff sign-in connectors (the Sign-in capability) implement `sign_in_url()`, which gives the provider address with the `state`, `nonce`, and S256 `code_challenge` core made, and `verify_sign_in()`, which checks the provider's answer, including the nonce and the ID token's signature, and returns a `StaffIdentity`: a stable `issuer` and `subject`, and an `email` with `email_verified` True only when the provider vouches for it. Core finds the staff account; never create one. Set `two_factor` only when the provider reports a second factor and the firm chose to accept it. In the contract tests, `sign_in_answer()` makes one fictional answer.

   An `auth` of `KeyExchangeAuth` is a PKCE sign-in with no app to register that hands back an API key, as OpenRouter's does. The connector builds the query with `authorize_query()` and turns the code into credentials with `exchange_code()`. The sign-in's `nonce` and everything it sent back (`returned`, such as an issued client ID) reach `exchange_code()` too. A provider that accepts only a loopback callback sets `loopback_redirect`: staff paste the unreachable address back, and core accepts only that address and a sign-in it started. Credentials with an `expires_at` are renewed through `refresh_credentials()`, under a row lock; raise `ConnectorError` with status 400 or 401 when the provider ended the sign-in. `button_label` sets the sign-in button's words where the provider requires its own.

   A payments connector whose account pays out to only one of the firm's accounts returns it from `deposit_accounts()`, so core never asks it for a link into the other. A webhook whose signing secret the provider issues when the endpoint is created (`secret_source="provider"` with a `register_label`) can be registered without a pasted secret: `register_webhook()` stores the provider's secret on the connection.

   A connector that waits on a provider's confirmation can set `flag`: it is offered only while `CONNECTOR_FLAGS` lists it, and its connections stop when the flag is removed. An AI connector billed to the staff member's own plan sets `usage_label` and `usage_url`, shown wherever staff choose the connection. A `CredentialField` with `required=False` may be left blank, such as a key a local server ignores.

   Automation connectors (the `automation` capability) must declare a `deliver_event` outbound action. Its payload is `{"id": ..., "event": {...}}`; the ID stays the same across retries. Core builds events in `crm/events/` and sends them through the outbound policy from the worker.

   An `OAuthAuth` can declare `app_fields`: non-secret values a superuser enters with the firm's app registration, such as Microsoft's directory (tenant) ID. The authorize and token URLs may name them in braces, such as `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize`; each value is validated and URL-encoded. Set `token_auth="basic"` for a provider that wants the app's ID and secret in an HTTP Basic header at its token endpoint, as Zoom does, instead of in the form.

   A credential field can set `generate` to a function that proposes a value, such as a signing secret, which the connect form shows once for staff to copy to the provider.

   `settings_fields` are non-secret choices made when connecting, such as sandbox or production. They are kept in `Connection.config` and shown on the connection's page; read them with `self.setting(name)`. A `SettingField` with `choices` is a list, and its `validate` function can reject a value, such as an address outside the provider's own domain.

   A setting with `options` gets its choices live, from `options(connector)`, such as the event types at the provider; it is chosen on the connection's page once connected, where staff who manage the connection can change any setting. With `multiple`, staff pick any number and the value is a list. For choices from the CRM's own data, call `crm.connect.lookups.options(name)`; core registers `lead_source_fields`, the contact choice fields a booking's tracking value can fill.

   Return typed records from `crm.connect.records` for your capability:
   - `CallRecord` (phone)
   - `TextRecord` (texting)
   - `EmailRecord` (mailbox)
   - `CalendarEventRecord` (calendar)
   - `BookingRecord` (scheduling)
   - `PaymentRecord` (payments)
   - `CaseStatusRecord` (case status)

   Core stores them:
   - calls, texts, and emails become communications, matched to a contact by E.164 phone number or email, or left for staff in Needs matching;
   - calendar events with a known contact, and bookings, become appointments linked through `ExternalRef`. A `LeadRecord` matches or creates a contact at the Inquiry stage and adds an intake note. A `BookingRecord` with `replaces` (the earlier booking's `external_id`) moves that appointment rather than making another. For a contact a booking creates, core fills the contact field named by the connection's `lead_source_field` setting from the tracking value named by `lead_source_from` (utm_source by default), when it matches one of the field's choices;
   - payments are recorded on the invoice they reference;
   - case status readings are recorded on the matter with that receipt number. A change creates a review task for staff. When the source cannot answer for a receipt number, the record carries a `problem` (`not_found`, `invalid_receipt`, or `unavailable`) instead of a status, and the matter shows it until a status arrives.

   Some sources answer questions rather than send new records. A case status connector asks core what to check by calling `crm.connect.lookups.due(capability, limit=...)` in `sync()`, which lists the receipt numbers of open matters not checked in the last 20 hours.

   Call providers through `crm.connect.http.ProviderClient`. It provides timeouts, retries that honour `Retry-After`, size limits, `paginate()`, and safe error messages. Set `min_interval` to space requests for a provider that limits requests per second. OAuth connectors call `self.access_token()`, which refreshes tokens under a lock and marks the connection for sign-in again when the provider refuses.

   Raise `ConnectorError` for provider failures. Its message is stored and shown to staff, so never include client data in it. Connectors never write to core models themselves: synced and webhook records reach features through the `crm.connect.signals.records_received` signal. When a sync has to stop early, such as at a rate limit, return what it has with `SyncBatch(..., warning=...)`. The run still counts, and staff see the warning on the connection.
3. Describe in `data_flows` exactly what the connector sends and receives, in plain language. Raise its `version` whenever that changes, so firms review it again.
4. Never call `perform()` from feature code. Use `crm.connect.policy.send()`, which applies the outbound policy (kill switch, preview mode, data-flow acknowledgment, personal-connection rules, admin-only setting, texting opt-outs, and hourly limit) and records every attempt.
5. Add a test case that mixes in `crm.connect.testing.ConnectorContract`, with `sync_examples`, `outbound_examples`, and `make_webhook()` as needed.
   - The contract tests check that each record is the typed record for its capability and that `record.problems()` is empty.
   - They block network access. Serve recorded, fictional `.test` fixtures through `crm.connect.http.use_transport(httpx.MockTransport(handler))`.
   - For a provider that signs inside the body, such as with a JWT, set `signature_in_headers = False` and test that an unsigned body is refused.
   - For a provider that proves an event with a shared value inside it rather than a signature, such as Microsoft Graph's `clientState`, also set `body_is_signed = False`. Such a connector must fetch what the event points to from the provider and never trust the body.
   - For a provider that sends no secret at all, such as LawPay, also set `webhook_uses_secret = False`. Treat each delivery only as a hint: read an ID, fetch the event from the provider, and record only what the provider returns.
   - Use phone numbers in the 555-0100 to 555-0199 range with a real area code, such as +1 202 555 0143.
6. Remove the provider's entry from `catalog.toml` once its connector exists. A wanted provider that already works through an installed connector, such as an email service that accepts SMTP, can say so with `works_through = "<that connector's key>"`; its tile then points there.

Ready connectors need a named maintainer. `connectors/example_oauth/` shows an OAuth connector; its sign-in flow runs through the firm's own app registration, entered by a superuser on the connector's page.
