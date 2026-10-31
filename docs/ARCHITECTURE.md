# Architecture

Open Immigration CRM is a conventional server-rendered Django application. Its deliberately small architecture is a security and maintenance feature.

## Components

```text
Browser
  │ HTTPS (operator-provided reverse proxy)
  ▼
Django + Gunicorn
  ├── server-rendered HTML, local CSS, and small local JavaScript
  ├── authenticated document download views
  ├── one-use public intake views
  └── Django admin for user and audit administration
        │
        ├── PostgreSQL (records, users, hashed intake tokens, audit events)
        └── private media volume (uploaded document bytes)
```

There is no Node build, SPA, Redis, Celery, message broker, AI provider, payment processor, email provider, or cloud-drive API. An optional `worker` process (`python manage.py run_worker`) runs connector syncs, webhook processing, and AI features from a queue kept in the application database; installations with nothing connected do not need it.

## Domain model

- `FirmProfile` is a singleton because one installation represents one firm.
- `Contact` represents a person in the inquiry/client lifecycle.
- `Matter` is case work with one primary contact and optional `MatterParty` records.
- `Activity` is an append-oriented note/status/event stream.
- `WorkItem` unifies a task, deadline, or appointment. An appointment's `meeting_url` holds its video meeting link, pasted by staff or made by a video connector.
- `TimeEntry` records tracked working time against a matter or contact; one timer runs per user, and no rates or amounts are stored.
- `ChecklistItem` is firm-authored and scoped to a single matter. An item that came from a workflow phase links to it (`ChecklistItem.phase`) and outlives it.
- `WorkflowTemplate`, `PhaseTemplate`, and `ItemTemplate` (in `crm/workflows/`) are firm-written workflows; none ship with the project. Applying one creates a `MatterWorkflow` with `MatterPhase`s and copies the items onto the matter's checklist, so later template edits do not change matters already using it.
  - One phase is in progress at a time.
  - A phase with due days puts a deadline in the work queue when it starts, and completing the phase completes that deadline.
  - A template can apply itself to new matters of one case type, through a `post_save` signal on `Matter`.
- `CustomField` (in `crm/customfields/`) defines a firm's extra contact or matter fields. Values live on the record (`Contact.custom_fields`, `Matter.custom_fields`, a JSON object keyed by each field's fixed `key`).
  - `CustomFieldsMixin` adds the active fields to `ContactForm` and `MatterForm`, so the web pages and the API validate them the same way.
  - Values of inactive fields are kept.
- `Invoice`, `InvoiceLine`, and `Payment` (in `crm/billing/`) hold invoices numbered when issued, lines marked earned fee or advance (trust), and payments recording the account they were deposited to. Each time entry can be billed once (`InvoiceLine.time_entry`), and voiding an invoice frees its time. `BillingSettings` and `StaffRate` hold rates and defaults. A `PaymentPlan` splits an issued invoice's balance into `PaymentPlanInstallment`s; payments apply to the earliest installment first, so installment status is derived rather than stored. Each installment has a reminder `WorkItem` that completes itself when the installment is paid, and only one plan per invoice is in force at a time. A plan with `email_reminders` on also has the worker email the client each installment's reminder once (`crm/billing/reminders.py`), recording on the installment when it was sent or why it was not. Billing pages require the `billing.access_billing` permission. Core keeps no trust ledger.
- `CaseNotice` and `CaseStatusCheck` (in `crm/notices/`) hold USCIS notices on a matter and receipt-number status readings. Receipt numbers are normalized (three letters, ten digits). A notice's response due date and appointment each drive one `WorkItem` that moves when the notice changes and is left alone once completed. A receipt notice fills the matter's blank receipt number, filing date, and priority date. A status reading that differs from the last one for the receipt is noted on the matter's activity. `CaseStatusProblem` keeps a receipt number a case status connector could not get a status for, until a status is recorded.
- `Communication` (in `crm/comms/`) is a call, text, email, or meeting linked to a contact and optionally a matter, logged by staff or recorded by a connector (stored once per connection and provider ID). `Contact.texting_opted_out_at` records a texting opt-out, which the outbound policy enforces.
- `Document` stores metadata in the database and bytes in private file storage.
- `IntakeForm` stores firm-created questions; `IntakeInvite` stores only a SHA-256 token hash; `IntakeSubmission` stores the received answers.
- `AuditLog` records important user actions. It is not a complete forensic event stream.
- `ExternalRef` (in `crm/connect/`) links a CRM record to the provider item it came from, for records with no provider-ID field of their own, such as appointments.
- `Connection`, `OutboundRequest`, `SyncRun`, `WebhookEvent`, `WorkerHeartbeat`, and `ConnectorApp` (in `crm/connect/`) hold connected provider accounts, the outbound log, sync history, received provider events, and worker check-ins; see Connectors below.
- `ApiSettings`, `ApiToken`, and `ApiRequestLog` (in `crm/api/`) hold the firm's API switches, scoped bearer tokens that act as one staff member (only a SHA-256 hash is stored), and a log row for every API request. See [API](API.md).
- `EventDelivery` (in `crm/events/`) is one CRM event queued for one automation connection. Model signals emit events after a change commits, whichever path made it. The worker sends each delivery through the outbound policy and retries failures with backoff. The payload is encrypted and deleted once the delivery settles. See [Outbound webhooks](WEBHOOKS.md).
- `StageChange` (in `crm/reports/`) records each time a contact enters a pipeline stage. It is written by a `post_save` signal on `Contact`. When the table was added, its history was rebuilt from the existing "Pipeline stage changed" notes. The operating dashboard (`/reports/`) counts consultations and retentions from it, by each contact's first entry into the stage. Every other figure on the dashboard is computed from existing records when the page loads; nothing else is stored.
- `TwoFactorDevice` holds a user's authenticator secret, encrypted with `FIELD_ENCRYPTION_KEYS`; `RecoveryCode` stores only SHA-256 hashes of single-use recovery codes.

## Trust boundaries

All ordinary CRM pages require an active staff account. Destructive document/party actions, firm settings, and exports require a superuser. A public intake URL is a bearer credential: possession allows one submission until the link expires or is revoked. Intake answers are displayed as unverified client-provided information.

The JSON API (`/api/v1/`) accepts only bearer tokens, never session cookies, so it needs no CSRF protection. A token acts as its staff member and is limited to its scopes. It is refused when:
- the API is off;
- the token is revoked or expired;
- its user is inactive or lacks the two-factor sign-in the firm requires;
- it belongs to a staff member while staff tokens are turned off.

Billing scopes also need the billing permission. Writes run in a transaction through the same forms and services as the web pages, and nothing can be deleted through the API.

Uploaded files are never routed through Django's public media URL. Downloads pass through an authenticated view and return `private, no-store`.

## Connectors

The [connector plan](CONNECTOR_PLAN.md) adds optional integrations without changing the shape of core. No provider connector has shipped yet. The framework is in place:

- `crm/connect/` is a provider-neutral Django app (label `connect`).
  - `registry.py` imports every module under the packages in `CONNECTOR_PACKAGES` (default `connectors`), plus installed packages that use the `open_immigration_crm.connectors` entry point. It rejects connectors whose manifest is incomplete or that were written for a connector API version this installation does not have (`API_VERSION`), and it merges installed connectors with the wanted providers listed in `connectors/catalog.toml`, each with a description, a link to the provider's developer documentation, and how its API is opened up. `manage.py startconnector` writes a skeleton package for a connector built outside this repository ([CONNECTOR_SDK.md](CONNECTOR_SDK.md)).
  - `Connection` is one connected account, either firm-wide or a single staff member's. Its credentials are encrypted with `FIELD_ENCRYPTION_KEYS`. New connections start in preview mode, and each records which version of the connector's data-flow description a superuser acknowledged.
  - `policy.send()` is the only path for outbound actions. It checks, in order:
    1. the site-wide kill switch
    2. that the connector is installed
    3. that the connection is active
    4. that the capability is enabled
    5. data-flow acknowledgment
    6. that a personal connection is used only by its owner, never by automations
    7. the admin-only setting
    8. recipient rules (no texts to a contact who opted out)
    9. the hourly limit
    10. preview mode

    It records every attempt as an `OutboundRequest`, storing only safe error text.
  - `testing.ConnectorContract` holds the shared contract tests. They block network access.
- `connectors/<provider>/` holds one optional adapter package per provider. `connectors/example/` is a fictional connector, hidden from the catalog, that exercises the framework. `connectors/webhooks/` is the built-in automation connector: it signs each event with the Standard Webhooks scheme and posts it to the firm's chosen address. `connectors/uscis_case_status/` checks receipt numbers through the firm's own USCIS developer app. `connectors/twilio/` and `connectors/dialpad/` send and receive texts and bring in calls. `connectors/google_workspace/` and `connectors/microsoft_365/` connect each staff member's Gmail and Google Calendar, or Outlook mail and calendar. `connectors/imap_smtp/` connects a staff member's mailbox at any other provider over IMAP and SMTP. `connectors/smtp/` sends the CRM's own email to clients through the firm's SMTP server or email delivery service. `connectors/oidc/` signs staff in through the firm's identity provider over OpenID Connect. `connectors/zoom/` makes a Zoom meeting for each of a staff member's appointments. `connectors/documenso/` sends matter documents for signature through Documenso and fetches the signed PDF; `connectors/example_esignature/` is a fictional e-signature service for tests. `connectors/google_drive/` copies matter documents to a folder per matter on the firm's Google Drive; `connectors/onedrive_sharepoint/` does the same in a SharePoint or OneDrive folder through Microsoft Graph. `connectors/example_storage/` is a fictional drive for tests. `connectors/calendly/` and `connectors/calcom/` file consultations booked through Calendly or Cal.com. `connectors/website_form/` takes signed submissions from the firm's website form as leads. `connectors/lawpay/` and `connectors/stripe/` make payment links and record payments, LawPay's into either account and each Stripe account's into the one its payouts reach. `connectors/anthropic/`, `openai/`, `openrouter/`, and `openai_compatible/` connect AI models; `connectors/example_ai/` is a fictional one for tests. `connectors/bia_edge/` searches BIA Edge's case law and authorities over its MCP server; `connectors/courtlistener/` searches CourtListener's case law with each staff member's own token. `connectors/example_research/` is a fictional research service for tests. `connectors/chatgpt_plan/` runs a staff member's own requests on their ChatGPT plan, behind the `chatgpt_plan` flag.

- **Worker.** `python manage.py run_worker` runs two things:
  - a scheduler thread that records a `WorkerHeartbeat` and queues a `SyncRun` for each due capability of each active connection;
  - the `django-tasks-db` worker, which runs syncs, webhook processing, and event deliveries from a queue table in the application database.

  Each sync resumes from the previous successful run's cursor. Synced and webhook records are typed records (`crm/connect/records.py`), delivered through the `records_received` signal.
  - `crm/comms/records.py` stores calls, texts, and emails, handles STOP and START replies, and links unmatched records when a contact gains that number or address. A later copy of a record adds to the stored one: the first time is kept, empty values erase nothing, new summary paragraphs are appended, and a text's delivery status only moves forward.
  - `crm/comms/texting.py` and `crm/comms/emailing.py` send texts and emails from contact and matter pages through any connection with the standard `send_text` or `send_email` action, via the outbound policy, and log each one under the provider's message ID.
  - `crm/comms/system_email.py` sends the CRM's own email to clients, such as secure intake links and payment plan reminders, through the firm's system email connection with the standard `send_system_email` action, via the outbound policy. Each sent email is logged on the contact with a summary the caller writes, so an intake link is never stored.
  - `crm/appointments.py` also puts each appointment on its staff member's connected calendars (`save_event` and `delete_event`), from the worker and through the outbound policy. It changes only events the CRM created, and ignores changes that came from a calendar, so nothing echoes back.
  - `crm/meetings.py` makes a video meeting for each appointment of a staff member who connected a video service (`save_meeting` and `delete_meeting`), from the worker and through the outbound policy, and keeps its join link on the appointment. Only the title and time are sent, and only when they change. The meeting is deleted when the appointment is deleted, unscheduled, or reassigned, and a link staff pasted is never replaced.
  - `crm/appointments.py` stores calendar events (only those with a known contact) and bookings.
  - `crm/leads.py` stores leads: a new person becomes a contact at the Inquiry stage with the lead source filled, and every submission is kept as an intake note.
  - `crm/billing/records.py` records payments. It never guesses the operating or trust account for an invoice with advance lines.
  - `crm/billing/links.py` asks a payment connector for payment links (`create_payment_link`): one per account still owed on an invoice, so advances are paid into trust and earned fees into operating. A connector says which accounts it takes payments into (`deposit_accounts()`), and is asked only for those. Links are kept on the invoice (`PaymentLink`) for staff to copy and send.
  - `crm/notices/records.py` records case status readings. An automatic reading that changes the status creates a review task (`crm/notices/alerts.py` sets its title and priority from USCIS's status title), and updates the receipt's open review task if there is one.

  Lookup connectors, such as case status, ask core what to check with `crm.connect.lookups.due()`. Core registers the answer: `crm/notices/tracking.py` lists receipt numbers on exactly one open matter that have not been checked in 20 hours, never-checked first. A sync that stops early returns what it has with a warning, which is shown on the connection.

  A record that fails to store is logged by kind and connection only, and the rest of the batch continues.
- **AI** (`crm/ai/`). `generate()` is the one way to ask a model, through any connection with the `generate` action, via the outbound policy. AI settings (off by default) turn AI and each registered feature on, and name the firm-wide connection for background work. `AIRequest` logs each request with the feature, the person, the connection, the model, and token counts, but no prompts or answers. The features are consult prep memos (`memos.py`), task suggestions (`suggestions.py`), and the assistant (`assistant.py`). `records.py` builds the CRM records they send. `jobs.py` holds their worker tasks, and queues the background features on each worker tick through the `worker_ticked` signal.
- **Legal research** (`crm/research/`). `services.search()` sends a search from a matter through any connection with the `search_authorities` action, via the outbound policy, and keeps the search and its results (`Search`, the latest 25 per matter). A connector lists what it can search in `search_scopes`. Staff save results, or add authorities by hand, as `Authority` records on the matter, kept once per matter and source.
- **E-signature** (`crm/signatures/`). From a matter, staff send one of its PDFs to the matter's own contacts through any connection with the `send_for_signature` action, via the outbound policy; a `SignatureRequest` keeps the provider's document ID, the signers, and where it stands. Providers report progress as `SignatureRecord`s: a signer signed, everyone signed (with the signed PDF, which is filed on the matter as a new `Document`), declined, or cancelled. Reports about documents the CRM did not send, and any after a request is finished, are ignored. Staff can cancel an open request (`cancel_signature`). Without a connection, staff upload the signed copy themselves.
- **Document storage** (`crm/document_storage.py`). With a firm-wide storage connection, the worker copies each matter's documents, one way, into a folder for the matter inside the folder the firm chose (`save_folder`, `save_file`), through the outbound policy. A new document is copied when added; a renamed matter renames its folder; deleting a document or matter moves its copy to the trash (`trash_item`). Copies are linked through `ExternalRef`, and a run every 15 minutes, recorded as a Document storage `SyncRun`, copies up to 25 documents that have no copy yet. Nothing is read back from the drive.
- **Matching** (`crm/matching.py`). Phone numbers are kept in E.164 form (`Contact.phone_e164`, `Communication.counterpart_key`), parsed with `PHONE_DEFAULT_REGION`. Emails are compared case-insensitively. A match must be exact and unique; there is no partial-number matching. Unmatched communications wait in the Needs matching list.
- **Provider calls** (`crm/connect/http.py`).
  - `ProviderClient` wraps `httpx` with timeouts, retries on 429 and 5xx that honour `Retry-After`, a response size limit, pagination, and staff-safe errors.
  - It requires HTTPS, except local addresses for connectors that opt in.
  - Tests swap in `httpx.MockTransport`.
  - `crm/connect/mcp_client.py` calls a service's tools over MCP (streamable HTTP, JSON or event-stream answers), through the same client.
  - OAuth token exchange and refresh use it too. `access_token()` refreshes under a row lock, saves rotated refresh tokens once, and marks the connection "Needs to reconnect" when the provider refuses.
- **Webhooks.** `/hooks/<token>/` accepts provider events.
  - The token is unique to each connection and stored only as a SHA-256 hash. The signing secret is stored encrypted.
  - An event is stored only after its signature verifies, and it is stored minimized and encrypted. It is answered quickly, deduplicated by provider event ID, and processed by the worker, which then deletes the payload.
  - Unknown tokens, inactive connections, bad signatures, oversized bodies, and malformed events are rejected without detail.

- **Connections page** (`/connections/`, in `pages.py`). It shows the catalog grouped by capability and the connect flow built from each manifest. The flow covers:
  - the data-flow description, which superusers must acknowledge;
  - credentials, either an API key or OAuth sign-in;
  - the capabilities to turn on;
  - an immediate connection test.

  Each connection has its own management page: health, recent syncs, events and outbound actions, preview and admin-only settings (superusers), webhook issue and rotation, and disconnect.
  - Staff can view firm-wide connections. Only superusers create or manage them. Staff create and manage their own personal ones.
  - The `connect_prompt` template tag puts a "Connect your …" prompt beside a feature's manual path. The dashboard shows connections that need attention and a stopped worker.
- **OAuth** (`oauth.py`). It uses the authorization-code flow with PKCE (S256) and a single-use state value that expires after 10 minutes.
  - It runs through the firm's own app registration (`ConnectorApp`; the client secret is encrypted), which a superuser enters on the connector's page alongside the exact redirect URI to register.
  - Sign-in starts from a link rather than a form, because `form-action 'self'` blocks a form submission that redirects to the provider.
  - Token exchange uses the standard library over HTTPS only, with a size limit.
- **Staff sign-in** (`sign_in.py`). Each active, firm-wide connection with the Sign-in capability whose data flows a superuser has reviewed adds a button to the sign-in page. Core keeps a one-time state, nonce, and PKCE verifier in the session for 10 minutes, and the connector turns the provider's answer into a `StaffIdentity` (`sign_in_url()` and `verify_sign_in()`).
  - Only an existing, active staff account can sign in. `SignInLink` records which one an identity signs in to: linked the first time by an email the provider vouches for, when exactly one account has it, and afterwards by the provider's issuer and subject alone.
  - The CRM's own two-factor step follows, as after a password, unless the identity reports the provider's second factor. Password sign-in is unchanged.

- **MCP server** (`crm/mcp/`, see [AI assistants](MCP.md)).
  - `/mcp` is a stateless, JSON-only Streamable HTTP endpoint written as plain Django views. It serves MCP 2026-07-28 and the earlier `initialize` handshake.
  - Sign-in uses django-oauth-toolkit as the OAuth authorization server, mounted at the root for the two `.well-known` documents and under `/o/`:
    - PKCE with S256 only;
    - tokens bound to the `/mcp` resource, and stored only as hashes;
    - rotating refresh tokens;
    - client registration by Client ID Metadata Documents, or by capped dynamic registration.
  - The consent step is the toolkit's view, limited to active staff, with the content security policy widened to the one client's callback.
  - Tools (`crm/mcp/tools.py`) run as the token's user, through the same forms and services as the web pages.
  - `McpSettings` keeps the server and write tools off until a superuser turns them on. `McpToolCall` logs each call without its arguments or results.

Operators can expose just `/hooks/` and the MCP paths through the reverse proxy without exposing the rest of the application.

Core never imports from `connectors/`; `scripts/check_scope.sh` enforces this.

## Extension policy

Provider code belongs in `connectors/`, never in core. New core dependencies require a clear security and maintenance benefit. A provider SDK may be added only as an optional extra for its connector. Schema changes use ordinary checked-in Django migrations. Operators should run migrations as a separate release step before replacing application processes.
