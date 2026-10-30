# Connectors

Each subpackage here is one optional provider adapter. Core code never imports these packages; `crm/connect/registry.py` loads them at startup, and installed third-party packages can add more through the `open_immigration_crm.connectors` entry point. The design is in [docs/CONNECTOR_PLAN.md](../docs/CONNECTOR_PLAN.md).

## Layout

```text
connectors/
  catalog.toml          wanted providers shown in the catalog before a connector exists
  dialpad/              Dialpad calls and texts, by signed webhook, with a call backfill
  example/              a fictional connector that tests the framework (hidden from the catalog)
    __init__.py         the connector class, registered with @register
    test_example.py     the shared contract tests plus connector-specific tests
  twilio/               Twilio texts, sent through the outbound policy and received by webhook
  uscis_case_status/    USCIS Case Status API, through the firm's own developer.uscis.gov app
  webhooks/             the built-in automation connector: signed events to any address
```

## Writing a connector

1. Subclass `crm.connect.base.Connector` and declare the manifest as class attributes: `key`, `name`, `description`, `maturity`, `capabilities`, `auth`, `connection_scope`, `data_flows`, `maintainers`, `outbound_actions`, and `settings_fields`. Register the class with `@register` from `crm.connect.registry`. A manifest that is incomplete is rejected at startup.
2. Implement `test_connection()`, plus:
   - `sync()` for the capabilities listed in `sync_capabilities` (the worker runs it every `sync_interval`);
   - `perform()` for outbound actions;
   - for providers that push events, a `webhooks` spec plus `verify_webhook()` (constant-time signature check; never accept unsigned requests), `parse_webhook()` (keep only what processing needs), and `handle_webhook()`.

   Webhook options in `WebhookSpec`:
   - `secret_source`: `"crm"` (the CRM generates the signing secret), `"provider"` (staff paste the provider's), or `"credential"` (the provider signs with a credential already entered, named by `secret_credential`, such as Twilio's Auth Token).
   - `verify_webhook()` also receives `url`, the address the request came to, for providers that sign it.
   - `register_label` offers, when staff turn webhooks on, to set the address at the provider through `register_webhook(url, secret)`. Say plainly what it changes there.
   - `ack_body` and `ack_content_type` are the answer to an accepted event, for providers that act on it. Twilio, for one, would text a plain-text answer back to the sender.

   Texting connectors declare a `send_text` outbound action. Its payload is `{"to": E.164 number, "body": text, "staff_email": the sender's email}`, and it returns the provider's message ID. Core's "Send a text" form uses it through the outbound policy and logs the sent text under that ID, so the provider's own copy and later delivery updates (`TextRecord.status`) land on the same communication. A `TextRecord` can carry `opt_out` (`stop` or `start`) when the provider recognized an opt-out reply itself.

   Automation connectors (the `automation` capability) must declare a `deliver_event` outbound action. Its payload is `{"id": ..., "event": {...}}`; the ID stays the same across retries. Core builds events in `crm/events/` and sends them through the outbound policy from the worker.

   A credential field can set `generate` to a function that proposes a value, such as a signing secret, which the connect form shows once for staff to copy to the provider.

   `settings_fields` are non-secret choices made when connecting, such as sandbox or production. They are kept in `Connection.config` and shown on the connection's page; read them with `self.setting(name)`. A `SettingField` with `choices` is a list, and its `validate` function can reject a value, such as an address outside the provider's own domain.

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
   - calendar events with a known contact, and bookings, become appointments linked through `ExternalRef`;
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
   - Use phone numbers in the 555-0100 to 555-0199 range with a real area code, such as +1 202 555 0143.
6. Remove the provider's entry from `catalog.toml` once its connector exists.

Ready connectors need a named maintainer. `connectors/example_oauth/` shows an OAuth connector; its sign-in flow runs through the firm's own app registration, entered by a superuser on the connector's page.
