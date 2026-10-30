# Connector framework plan

Status: draft, updated 2026-10-29 with decisions and AI access. Not committed.

## Goal

Make Open Immigration CRM connectable to the products firms already use, without making it hard
to run or audit. A new installation shows every automated feature as **"Connect your …"**, and the
firm picks its own provider for each one from a large, visible catalog: phone system, email,
calendar, payments, e-signature, AI and more.

The CRM should stay fully usable with nothing connected. A connection automates work that staff
can already do by hand.

## Principles

1. **Manual first.** Every feature works without a connection. Calls can be logged and payments
   recorded by hand. A connection only adds automation.
2. **Capabilities, not vendors.** Core code talks to a capability, such as `phone` or `payments`.
   Vendors are adapters behind that interface. Core never imports a vendor module.
3. **Honest catalog.** Each tile is labeled Ready, Beta or Wanted. A tile is never marked Ready
   unless it has a maintainer and passing contract tests.
4. **Disclose data flows.** Each connector says in plain language what it sends and what it
   receives. A superuser acknowledges that before it is enabled, and the acknowledgment is
   audited.
5. **Safe outbound defaults.** Anything that leaves the CRM (sending email or texts, charging a
   card, writing a calendar event, calling an AI model) starts in preview mode and passes a policy
   check.
6. **Fail closed.** If a webhook signature, credential or scope is missing or invalid, the request
   is rejected. That includes development settings.
7. **Self-hosting stays simple.** No Redis or broker. There is one optional worker process, and it
   is needed only once something is connected.

## Phase 0: reset the product boundary

The repo described a core with no integrations. These files change together, in one reviewed
pull request, before any connector code lands:

| File | Change |
|---|---|
| `README.md` | Add a "Core and connectors" section: core stays small, connectors are optional and off until connected, and none has shipped yet. |
| `docs/ROADMAP.md` | Move integrations from non-goals to a connector roadmap. Keep the real non-goals: autonomous legal work, shipped legal content, multi-tenant SaaS, queue infrastructure and telemetry. |
| `CONTRIBUTING.md` | Add a connector contribution path: manifest, contract tests, fixtures, a named maintainer and license rules. |
| `docs/ARCHITECTURE.md` | Describe the planned framework, connector packages, worker and public paths. |
| `docs/SECURITY_MODEL.md` | Describe how connectors change the risk picture, and the controls that must land before the first one ships. |
| `docs/PRIVACY.md` | State that connectors send data only to providers the firm connects, and that telemetry stays excluded. |
| `scripts/check_scope.sh` | Provider names are allowed only in `connectors/`, queue infrastructure stays banned everywhere, and core must not import from `connectors/`. |
| `scripts/check_privacy.sh` | Already provider-neutral: firm markers live in an ignored `check_privacy.local`. Add API-key formats for AI, payment and OAuth providers. |

## Capabilities and catalog

**Ready at 1.0** = shipped and tested. **Later** = Beta or Wanted tiles. Bold names are adapters
that already exist in the firm's private CRM and would be ported.

| Capability | Works by hand in core | When connected | Ready at 1.0 | Later |
|---|---|---|---|---|
| Phone | Log calls | Calls are logged automatically, with recordings and recaps | **Dialpad**, Twilio | RingCentral, Aircall, Zoom Phone, Vonage, 8x8, Nextiva, Telnyx, Teams Phone |
| Texting and messaging | Log texts | Send and receive texts; STOP opt-out handling | **Twilio**, Dialpad | RingCentral, WhatsApp Business, Telnyx |
| Mailbox | Log emails | Inbox matched to contacts; send from the CRM | **Google Workspace**, **Microsoft 365**, IMAP/SMTP | Zoho Mail |
| System email | Staff copy intake links by hand | The CRM emails intake links, reminders and 2FA backup codes | SMTP | Postmark, Amazon SES, SendGrid, Mailgun |
| Calendar | Appointments as work items | Two-way calendar sync | **Google**, **Microsoft 365** | CalDAV |
| Scheduling | Create consults by hand | Bookings create the contact and appointment | **Calendly**, Cal.com | Acuity, Microsoft Bookings, Zoom Scheduler |
| Video meetings | Paste a link | A meeting link is created with the appointment | Zoom | Teams, Google Meet, Jitsi |
| Payments | Record payments; invoice PDFs | Payment links and card/ACH; separates trust from operating funds | **LawPay**, Stripe | Square, PayPal, Headnote, Confido Legal |
| Accounting | CSV export | Invoices and payments pushed to the books | — | QuickBooks Online, Xero, FreshBooks |
| Document storage | Private local storage | Matter folders mirrored to the firm's drive | **Google Drive** | OneDrive/SharePoint, Dropbox, Box, Egnyte |
| E-signature | Upload the signed copy | Send for signature; the signed PDF is filed automatically | Documenso | DocuSign, Dropbox Sign, Adobe Acrobat Sign, BoldSign |
| AI models | — (off) | Firm-chosen model for drafts, summaries and prep memos | **OpenAI** and **Anthropic** (API keys), OpenRouter, any OpenAI-compatible endpoint (covers local models) | ChatGPT plan through Sign in with ChatGPT (Beta until OpenAI confirms), Azure OpenAI, Gemini, AWS Bedrock, Google Vertex AI |
| Translation | — | Translate client messages and intake answers | — | DeepL, Google Cloud Translation, or an AI model |
| Legal research | — | Authority search from a matter | **BIA Edge**, CourtListener | others |
| Case status | Enter status by hand | Receipt numbers checked automatically, with alerts | **USCIS Case Status API** | Visa Bulletin (public data), USCIS processing times |
| Shipping | Paste a tracking number | Delivery of filing packages tracked | — | USPS, FedEx, UPS |
| Lead capture | Create leads by hand | Website and ad leads arrive as contacts | Signed website-form webhook | WordPress form plugins, Google and Meta lead forms, Google Local Services Ads |
| Marketing analytics | — | Spend and source reporting | — | **Google Ads**, **GA4**, Meta Ads |
| Sign-in (SSO) | Local accounts + TOTP | Sign in with the firm's identity provider | Generic OIDC with Google and Microsoft Entra presets | Okta, JumpCloud |
| Migration | CSV import | Move records over from an old system | CSV templates | Docketwise, INSZoom, Clio, MyCase, LawLogix |

Some pieces are core features rather than connectors, but they make the CRM connectable from the
outside: a REST API with scoped tokens, signed outbound webhooks (which n8n, Zapier and Make can
use) and an MCP server. The MCP server is also how firms use their own Claude or ChatGPT
subscriptions with the CRM; see "AI access".

## Architecture

### Layout

```text
crm/                      core app; never imports vendor code
crm/connect/              framework: registry, models, policy, webhooks, OAuth, jobs, UI
connectors/<key>/         first-party connectors, one package each, optional
connectors/catalog.toml   Wanted and Beta entries that have no code yet
```

Third-party connectors install as Python packages and register through an entry point
(`open_immigration_crm.connectors`). The registry merges three sources: first-party connectors,
installed packages and `catalog.toml`.

Each connector declares its vendor SDK as an optional extra, for example
`pip install open-immigration-crm[stripe]`. The default is plain HTTPS through one shared client,
and tests use its built-in mock transport.

### Connector definition

Every connector is declarative. The catalog, the connect flow and the data-flow disclosure are all
rendered from these definitions:

```python
@register
class DialpadConnector(Connector):
    key = "dialpad"
    name = "Dialpad"
    maturity = Maturity.READY
    maintainers = ["@handle"]
    capabilities = {Capability.PHONE, Capability.TEXTING}
    auth = ApiKeyAuth(docs_url="https://...")
    connection_scope = ConnectionScope.FIRM
    data_flows = DataFlows(
        version=1,
        receives=["call records", "recording links", "text messages", "call recaps"],
        sends=["text messages staff approve"],
    )
    logo = "connectors/dialpad/logo.svg"

    def adapter(self, connection: Connection) -> PhoneAdapter: ...
```

Logos are bundled locally, which fits the current `img-src 'self'` CSP. A tile with no logo
shows a monogram. A "not affiliated with or endorsed by" notice appears on the Connections page.

### Core tables

| Model | Purpose |
|---|---|
| `Connection` | One connected account. Holds the connector key (a registry string, not a choices enum), a label, firm or per-user scope with an owner, a status, the enabled capabilities, encrypted credentials, non-secret config, granted scopes, a hashed webhook token, an encrypted webhook secret, a preview-mode flag, the data-flow acknowledgment (who, when, which version) and last success/error. |
| `ConnectorApp` | Instance-level OAuth client ID and encrypted secret for "bring your own app" providers. Superuser only. |
| `ExternalRef` | Generic link from a CRM record (content type plus a string object ID, so UUID and integer keys both work) to a provider ID, with thread/parent ID and etag. Unique per connection and external ID. Replaces the per-type reference tables in the firm CRM. |
| `SyncRun` | Connection, capability, cursor, start and finish times, counts and a redacted error. |
| `WebhookEvent` | Connection, provider event ID or body hash (unique for idempotency), signature result, minimized and encrypted payload, processing status and attempts. |
| `OutboundRequest` | Every outbound action, with actor, automation flag, policy decision code, preview flag, result and provider reference. |
| `WorkerHeartbeat` | Lets the UI warn when syncs are paused because the worker is not running. |

New core models for the manual-first features are listed under "Features that work without
connectors" below.

### Capability interfaces

Each capability has a typed interface and normalized records, generalizing the firm CRM's
`NormalizedMessage` and `NormalizedCalendarEvent`:

```python
class PhoneAdapter(Protocol):
    def verify_webhook(self, request: HttpRequest) -> bool: ...
    def parse_event(self, payload: dict) -> list[NormalizedCall | NormalizedText]: ...
    def calls_since(self, cursor: SyncCursor) -> Iterable[NormalizedCall]: ...
    def send_text(self, *, to: str, body: str) -> ProviderRef: ...
```

Every interface method a connector supports must pass a shared contract test for that
capability.

### Authentication modes

| Mode | Used for | New-user experience |
|---|---|---|
| API key or token | Dialpad, Twilio, Stripe, OpenAI, Anthropic, BIA Edge, Calendly | Paste a key, the CRM runs a test call, done |
| OAuth with the firm's own app | Google, Microsoft, Zoom, DocuSign, QuickBooks | A wizard shows the exact redirect URI and scopes to copy into the provider console, then "Sign in with …" |
| Hosted connection broker (optional, later) | Same OAuth providers | One-click sign-in, built after Phase 4; see "Decisions" |
| Per-user AI plan | ChatGPT plan through Sign in with ChatGPT | Each staff member clicks "Continue with ChatGPT"; see "AI access" |
| One-click pay-per-use AI | OpenRouter (OAuth with PKCE) | The user signs in and OpenRouter issues a key that the user controls |
| Public data | Visa Bulletin, processing times | "Turn on" with no credentials |
| Inbound webhook | Website forms, generic sources | The CRM generates a URL and a signing secret |

OAuth uses PKCE and a hashed one-time `state`, and asks only for the scopes of the capabilities
the firm turns on. Google Workspace firms can mark their own app as internal, which avoids
Google's verification for restricted Gmail scopes.

### Webhooks

- One endpoint per connection: `/hooks/<connection token>/`. The connector names the capability of each event when it parses it.
- Each connector verifies its own signatures (HMAC, JWT, or Microsoft Graph `clientState` with
  the subscription ID).
- Invalid or unsigned requests are rejected in every environment.
- Events are stored minimized, deduplicated by event ID, answered with a fast 2xx and processed
  by the worker.

**Reachability conflicts with the current security advice**, which tells firms to keep the CRM
behind a VPN. Document three options:

1. Expose only `/hooks/` publicly through the reverse proxy.
2. Use polling, for providers that support it.
3. Relay events through the broker, if one is built.

The connect flow tells the firm which of these each connector needs.

### Background jobs

Use the `django-tasks` backport on Django 5.2 LTS, with its database-backed backend and a
`worker` service in `compose.yaml`. It has the same API as `django.tasks` in Django 6.x, so
moving to the next LTS later only changes imports. Verify the backport's current state before
adopting it.

A small scheduler command queues periodic syncs. With no active connections, the worker does
nothing, and installations without connections can leave it out.

### Outbound policy

This generalizes `safety.py` from the firm CRM to every outbound action. Checks run in this
order, and each result is recorded on `OutboundRequest`:

1. Environment kill switch
2. Connection is active and the capability is enabled
3. Preview mode (new connections start here; a superuser turns on live sending)
4. Actor role: manual sends can be restricted to admins
5. Recipient rules: SMS opt-out, missing consent, recipient not linked to a contact
6. Rate limits

AI requests go through the same gate. Each AI feature is opt-in, and AI output is labeled as a
draft that needs attorney review.

### Matching into CRM records

- Phone numbers are normalized to E.164 and emails are lowercased, both indexed on `Contact`.
- Unmatched calls, texts and emails go to a "Needs matching" queue. Creating a lead from an
  unknown caller is a setting, off by default.
- Every synced record carries an `ExternalRef`, so re-syncs update rather than duplicate.
- A dedupe tool ships from the start. The firm CRM needed `dedupe_appointments` after the fact.

## AI access

All AI features are opt-in and off by default. A superuser turns on each feature separately
(consult prep memos, task suggestions, the assistant). Nothing goes to a model until both the
feature and an AI connection are on.

Firms can power AI in four ways. This reflects each vendor's published rules as of 2026-10-29:

| Path | Who pays | Scope | Usable by a self-hosted web app today |
|---|---|---|---|
| Bring the CRM into Claude or ChatGPT through the MCP server | The user's own Claude or ChatGPT plan | Per user | Yes. Claude custom connectors work on Free (one connector), Pro, Max, Team and Enterprise. ChatGPT custom MCP connectors are confirmed for Business, Enterprise and Edu on the web; Free, Plus and Pro are unconfirmed (checked 2026-10-30). |
| Sign in with ChatGPT, using plan usage | The user's ChatGPT plan | Per user | Not confirmed; see below |
| OpenRouter sign-in | OpenRouter pay-per-use credits | Per user or firm-wide | Yes |
| API key or local model | The firm | Firm-wide | Yes |

### Claude subscriptions: through the MCP server only

Anthropic does not allow the "sign in with your Claude plan" approach. Its Claude Code legal and
compliance page bars third-party apps from:
- offering Claude.ai login,
- routing requests through Free, Pro or Max plan credentials,
- storing or passing along Claude.ai credentials or session tokens.

Developers are told to use API keys from Claude Console or a supported cloud provider.

So the CRM will not offer a Claude sign-in and will not accept pasted Claude subscription tokens.
A firm that wants to use its Claude plan adds the CRM's MCP server as a custom connector in
Claude. Claude does the AI work under the user's own plan and calls the CRM's tools, so the CRM
never holds Anthropic credentials. For Claude inside the CRM's own features, the firm uses an
Anthropic API key, or Claude through AWS Bedrock or Google Vertex AI. The Anthropic tile says all
of this.

### ChatGPT plans: build behind a flag, confirm with OpenAI

Sign in with ChatGPT lets a signed-in user's requests run against their own ChatGPT plan. The
developer docs name Plus and Pro. What the docs describe:

- **Who can use it.** Plan usage is offered for "open-source and locally hosted apps." A paid or
  remotely hosted app is directed to OpenAI's interest form instead.
- **Callback.** The OAuth callback must be a loopback address on `127.0.0.1`. For an app on a
  remote server, the user signs in on their own computer and the credentials are copied to the
  server. The docs do not describe a web app on the firm's own domain with many staff signing in
  from their browsers. On that copied-credentials path, OpenAI says it cannot yet revoke plan
  access for a single server.
- **Registration.** Each registration is tied to one user and one workspace. There is no client
  secret. The flow uses PKCE and a stable host ID. Access tokens last one hour, and refresh
  tokens last 30 days and rotate on each refresh.
- **Requests.** Requests go to the Responses API with `store: false` and streaming. Function
  tools work. Hosted tools such as file search and code interpreter do not.
- **Limits and errors.** Users set per-app limits in their ChatGPT settings. Plus users share a
  five-hour limit across every app they use. A limit error stops the request, and OpenAI never
  switches to another billing method.
- **Not stated.** The docs say nothing on retention or confidentiality beyond `store: false`, or
  on whether Business and Enterprise workspaces are eligible.

The plan for this path:

1. Ask OpenAI the questions under "Open questions".
2. Meanwhile, build the connector as Beta behind a flag:
   - a per-user `Connection`,
   - token refresh that never runs twice at once,
   - OpenAI's required "Continue with ChatGPT" button, plus a "Using ChatGPT plan" label with a
     "Manage usage" link,
   - handling for OpenAI's plan-usage error codes.
3. Mark it Ready only after OpenAI confirms this use in writing and its data terms are known.

### Rules for per-user AI plans

- A user's plan runs only that user's own interactive requests. It never runs another staff
  member's request or a background job.
- Background AI, such as overnight prep memos, needs a firm-wide connection: an API key,
  OpenRouter or a local model.
- Each AI feature declares whether it is interactive or background, and the outbound policy
  enforces the routing.
- Every AI request records which connection paid for it.

### Confidentiality

- Consumer plans (ChatGPT Plus and Pro, Claude Pro and Max) carry different data-use terms from
  business plans and API access (ChatGPT Business and Enterprise, Claude Team and Enterprise,
  both APIs).
- The AI connect flow and the MCP setup guide recommend business plans or API access for client
  data, and link to each vendor's terms.
- A local model keeps everything on the firm's own hardware.

### MCP server

This moves up from Phase 5 to Phase 3.
- **Sign-in.** Uses the MCP authorization flow, so Claude's "register automatically" option and
  ChatGPT's authentication setup both work.
- **Permissions.** Each staff member signs in as themselves and sees only what their CRM role
  allows.
- **Tools.** Read tools come first: search contacts, matter summary, deadlines. Write tools
  (create a task, log a note, save a draft) require confirmation, stay off until a superuser
  turns them on, and are audited on every call.
- **Reachability.**
  - Claude connects from Anthropic's cloud, so the MCP endpoint must be public or allow
    Anthropic's IP ranges.
  - ChatGPT offers a Secure MCP Tunnel for private servers.
  - Otherwise, use the same expose-only-this-path guidance as webhooks.
- **Sampling.** Do not use MCP sampling. The 2026-07-28 spec deprecates it.
- **Detail.** See "Phase 3 plan", PR 3.1. Checked 2026-10-30 against:
  - the [MCP authorization spec](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization);
  - Claude's [connector authentication](https://claude.com/docs/connectors/building/authentication) and [review criteria](https://claude.com/docs/connectors/building/review-criteria) docs;
  - OpenAI's [Apps SDK authentication](https://developers.openai.com/apps-sdk/build/auth) and [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) docs;
  - the [django-oauth-toolkit](https://django-oauth-toolkit.readthedocs.io/en/latest/) docs.

Sources, read 2026-10-29:
- [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
- [Claude custom connectors](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)
- Sign in with ChatGPT developer docs:
  - [open source](https://developers.openai.com/siwc/token-sharing-open-source)
  - [sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
  - [self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
  - [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [ChatGPT custom MCP servers](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenRouter OAuth PKCE](https://openrouter.ai/docs/use-cases/oauth-pkce)
- [MCP sampling (deprecated)](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)

## User experience

**Connections page** (`/connections/`)
- Grouped by capability, with search and Ready, Beta and Wanted filters.
- Tiles show the logo, a one-line description, the auth type ("API key" or "Sign in") and a
  status badge once connected.
- Wanted tiles link to that connector's public GitHub discussion. The CRM sends no telemetry and
  never phones home.

**Connect flow**
1. Tile
2. Detail page: what it does, data sent and received, permissions requested, and whether it needs
   public webhooks
3. Acknowledge the data flows
4. Enter credentials or sign in
5. The test call passes
6. Choose capabilities and defaults, for example which mailbox, or which deposit account takes
   trust money and which takes operating money
7. Connected in preview mode, and the first sync is queued

**Empty states.** Each automated area shows the connect prompt next to the manual path:

| Screen | Empty state |
|---|---|
| Calls | "Connect your phone system to log calls automatically" · Log a call |
| Billing | "Connect a payment processor to accept cards and ACH" · Record a payment |
| Documents | "Connect e-signature to send retainers for signature" · Upload signed copy |
| Matter status | "Connect USCIS case status to check receipts automatically" |
| Calendar | "Connect your calendar to sync appointments" |

**Health**
- Each connection shows its status, last sync and recent errors, with a Reconnect button when
  it needs reauthorization.
- A dashboard banner appears for broken connections and for a stalled worker.
- Disconnecting revokes the token at the provider where possible, deletes the credentials, and
  asks whether to keep or remove the synced records.

**First-run checklist.** A skippable list on the dashboard: firm profile, 2FA, email, calendar,
phone, payments.

## Security and professional responsibility

- Port `EncryptedJSONField` (MultiFernet with key rotation). `check --deploy` fails when a
  connection exists without `FIELD_ENCRYPTION_KEYS`.
- Built-in TOTP 2FA with recovery codes. Keep a local superuser break-glass login when SSO is on.
- Portable JSON export includes connection metadata but never credentials or secrets.
- Webhook payloads are minimized before storage (port the firm CRM's data-minimization helpers).
- Use the provider's hosted checkout and signing pages by redirect, so the staff CSP stays
  strict and no vendor scripts load.
- Data-flow acknowledgments support the firm's own confidentiality analysis (Model Rule 1.6(c);
  ABA Formal Opinion 512 for generative AI). The software does not make that judgment for the
  firm.
- Payments connectors must state whether they can keep trust and operating funds separate. A
  connector that cannot must say so on its tile. Version 1 records which account a payment went to
  and does not attempt a full trust ledger.
- Texting must handle STOP and opt-out in core. US business texting may need carrier (10DLC)
  registration. The connect flow says so.

## Features that work without connectors

These are ported from the firm CRM as core features before their connectors, so every empty state
has a working manual path:

1. Communications log for calls, texts, emails and meetings, with a texting opt-out
2. Billing: invoices, line items flagged as earned fee or advance, manual payments, rates on the
   existing time entries, and payment plans with reminders as work items
3. USCIS notices and receipt tracking on matters
4. Workflow templates, firm-authored, with none shipped
5. Custom fields on contacts and matters
6. Engagements: one consult or retainer covering several people (deferred until after Phase 3)
7. An operating dashboard built from CRM data only
8. REST API with scoped tokens and an API audit log
9. Signed outbound webhooks

## Phases

| Phase | Scope | Done when |
|---|---|---|
| 0. Boundary | Docs and check scripts listed above | Release gate passes under the new rules |
| 1. Foundation | Encryption, TOTP, worker and scheduler, core tables, registry and manifests, `catalog.toml`, Connections UI, API-key and BYO-OAuth flows, webhooks, outbound policy, data-flow acknowledgment, an `example` connector, and the contract test harness | The example connector can be connected, receive a signed webhook, sync, attempt a preview-mode send and disconnect, all audited. No vendor code yet. |
| 2. Manual-first features (done) | The features above except engagements, each with its empty state; see "Phase 2 plan" | Every capability's "Connect your ..." prompt sits beside a working manual path |
| 3. Port the firm's adapters, plus AI access | Google, Microsoft 365, Dialpad, Twilio (texting), Calendly, LawPay, OpenAI, Anthropic, USCIS Case Status and BIA Edge, rewritten against the interfaces with firm-specific behavior removed. AI access: the MCP server, OpenRouter, the OpenAI-compatible endpoint, and the ChatGPT plan connector behind a Beta flag | Each connector passes its contract tests on recorded fixtures and has its data flows filled in. A staff member can work with the CRM from Claude through the MCP server. |
| 4. Second adapters | IMAP/SMTP, Twilio voice, Dialpad texting, Stripe, Cal.com, Documenso, Zoom, CourtListener, Google Drive and OneDrive/SharePoint, the website-form webhook, OIDC sign-in and SMTP system email | No interface needed a vendor-specific escape hatch; any interface fixes were made in core and the first adapter was updated |
| 5. Ecosystem | Connector SDK guide and template, entry-point packages, filled-out Wanted catalog, n8n/Zapier/Make recipes, migration importers, WhatsApp, shipping, translation | An outside contributor can ship a Beta connector without touching `crm/` |
| 6. Broker | Hosted OAuth broker and webhook relay | Starts after Phase 4 (see "Decisions") |

## Phase 2 plan

Each feature is its own Django app inside `crm/` (like `crm/connect/`), so features keep separate
migrations and stay out of `crm/views.py`. Each works fully by hand, shows its "Connect your ..."
prompt, and stores a source connection and provider ID so Phase 3 connectors need no schema change.

| PR | Scope | Unblocks in Phase 3 |
|---|---|---|
| 2.1 Communications log (`crm/comms`, done) | Calls, texts, emails and meetings on contact and matter pages and a list page; per-contact texting opt-out enforced by the outbound policy; existing call and meeting activities moved into the log | Phone, texting, mailbox and calendar connectors |
| 2.2 Billing (`crm/billing`, done) | Staff and firm hourly rates; invoices numbered at issue; line items flagged earned fee or advance; unbilled time added as lines, rounded up to the firm's increment, each entry billed once; manual payments recording operating or trust deposit; printable invoice page; Billing permission | Payment connectors |
| 2.3 Payment plans (done) | Monthly, biweekly, or weekly installments over an invoice's balance (even split, leftover cents last); payments apply oldest first; each installment gets a reminder work item that completes when paid; canceling removes open reminders; voiding the invoice cancels the plan | Automated reminders |
| 2.4 USCIS notices (`crm/notices`, done) | Notice records (receipt, biometrics, interview, RFE, NOID, approval, denial, rejection, transfer, card) linked to the matter's scanned document; response due dates become deadlines (RFE high, NOID urgent) and appointments become calendar items that follow edits; receipt notices fill blank matter receipt and dates; manual status history, with changes noted on the matter | USCIS Case Status connector |
| 2.5 REST API (`crm/api`, done) | `/api/v1/` for contacts, matters, work, notes, communications, time, notices, status checks, and invoices (read only), using the web forms' validation; tokens act as one staff member, are scoped per resource, hashed, expire within a year, and can be revoked; the API is off until a superuser turns it on; every request is logged | MCP server |
| 2.6 Outbound webhooks (`crm/events`, `connectors/webhooks`, done) | An Automation capability and a built-in Webhooks connector. CRM events are emitted from model signals after commit. Each connection chooses its events and whether to send IDs and links or full records. Deliveries are signed with Standard Webhooks, pass the outbound policy, start in preview mode, run in the worker, and retry with backoff for about a day and a half | n8n, Zapier and Make recipes |
| 2.7 Workflow templates and custom fields (`crm/workflows`, `crm/customfields`, done) | Firm-written workflows: phases with checklist items and optional phase deadlines, written as a text outline. They are applied by hand or automatically to new matters of a case type, and a `matter.phase_started` event fires on each phase. Custom contact and matter fields (text, long text, number, date, choice, yes or no) appear on forms, record pages, exports, the API, and webhook records | — |
| 2.8 Operating dashboard (`crm/reports`, done) | `/reports/` and `/api/v1/reports/summary/`, built only from CRM data, for any period, compared with the previous one. Covers: intake (from a new stage history, backfilled from existing notes) with lead sources from a choice custom field; matters by status, case type, and workflow phase; deadlines and open RFE or NOID responses; USCIS notices; communications; time; and, with billing access, invoiced, collected, and outstanding by age | — |

Decided 2026-10-29:
- Engagements wait until after Phase 3; they move the pipeline off the contact and nothing in
  Phase 3 depends on them.
- Billing is visible to superusers and staff granted a Billing permission; everyone can log time.
- The API uses plain Django views, not an API framework.
- Invoices print from an HTML page; no PDF library.
- Trust accounting stays at recording which account a payment went to.

## Phase 3 plan

Phase 3 connects real providers. It is done when every connector passes its contract tests on
recorded, fictional fixtures and has its data flows filled in, and when a staff member can work
with the CRM from Claude through the MCP server.

The firm CRM's adapters were surveyed on 2026-10-30. They are rewritten against the framework,
not copied: firm names, accounts, and business rules come out, and the bugs listed below are not
carried over.

| PR | Scope |
|---|---|
| 3.1 MCP server (`crm/mcp`, done) | Off until a superuser turns it on. **Endpoint**: `/mcp/`, written as plain Django views (the official SDK needs ASGI). It is stateless, answers with JSON only, checks `Origin`, and supports both protocol versions in use: 2026-07-28 (`server/discover`, version headers, no sessions) and the earlier version that Claude's docs still describe (`initialize`, `notifications/initialized`, `ping`). **Sign-in**: `django-oauth-toolkit` as the authorization server, which is tested for this role. It publishes authorization server metadata (RFC 8414) and protected resource metadata (RFC 9728), and returns a 401 with `WWW-Authenticate` to start sign-in. It accepts S256 PKCE only and binds tokens to the `resource` (RFC 8707), and refresh tokens rotate. Clients register through Client ID Metadata Documents (Claude's published identity and ChatGPT) or dynamic registration (Claude's "register automatically"), which is rate-limited. Each staff member approves access on a consent page after normal sign-in, including two-factor. **Tools** follow the API's scopes and the person's own permissions. Read tools: search contacts and matters, matter summary, deadlines, notices, communications, time, and invoices (with billing access). Write tools are off until a superuser turns them on: create a task, add a note, log a communication, record a status check. Every tool carries `title` and `readOnlyHint` or `destructiveHint`, so Claude and ChatGPT ask before writes. Every call is audited. Clients and tokens are listed on the API access page, where they can be revoked. **Setup guide** (`docs/MCP.md`): adding the connector in Claude and in ChatGPT. Reachability: `/mcp/`, `/o/`, and `/.well-known/` must be reachable from Claude (160.79.104.0/21) or ChatGPT (OpenAI's published list), and the consent page from staff browsers. ChatGPT's Secure MCP Tunnel does not carry the authorization server. |
| 3.2 Provider framework (done) | A shared HTTPS client (`httpx`): timeouts, retries that honour `Retry-After` on 429 and 5xx, response size limits, and a pagination helper. Tests use its mock transport with the network blocked. OAuth token refresh under a row lock before expiry; rotating refresh tokens are saved once; a refused refresh marks the connection "needs sign-in again". Typed records per capability (call, text, email, calendar event, booking, payment, case status) with a contract test suite each, replacing the free-form `Record.data`. Core handlers that store them: communications, appointments, payments, status checks. An `ExternalRef` table for records with no provider-ID field of their own, such as appointments. Matching: phone numbers stored in E.164 with a firm default country (`phonenumbers`) and emails lowercased, both indexed. Unmatched calls, texts, and emails go to a "Needs matching" list where staff link or create the contact. There is no partial-number matching. |
| 3.3 USCIS Case Status (`connectors/uscis_case_status`, done) | The firm's own developer.uscis.gov app (client credentials), with a sandbox or production setting. **Sandbox**: only USCIS's published test receipt numbers, once a day, which builds the days of traffic USCIS asks for before production access; client receipt numbers never go to the sandbox. **Production**: the worker checks the receipt numbers of open matters about once a day, two requests a second. A 429, or three receipt numbers in a row without an answer, stops the run, which keeps what it has. Each reading is recorded on its matter. A change creates a review task for staff, prioritized by the kind of change: no legal interpretation and no message to the client. Receipt numbers USCIS cannot answer for stay on the matter with USCIS's message, including the 8 U.S.C. 1367 caveat for "not found". **Framework additions**: connection settings (`SettingField`); `lookups.due()`, so connectors ask core what to check; `SyncBatch.warning`; and request spacing in the shared client. Setup guide: `connectors/uscis_case_status/README.md`. |
| 3.4 Phone and texting: Dialpad, Twilio (`connectors/dialpad`, `connectors/twilio`, done) | **Dialpad**: calls with recording links, voicemails, and Dialpad's AI recap arrive by webhook as HS256 JWTs, read only from the verified token. A call's separate events build one communication. An hourly sync catches up on calls from Dialpad's call list. Texts arrive both ways. Dialpad has no API for past texts, so only calls are backfilled. Turning on webhooks can create the Dialpad webhook (with a CRM secret) and company-wide call and text subscriptions; a new address or disconnecting deletes the old webhook. **Twilio**: texts only, from a number or Messaging Service, sent through the outbound policy. Incoming texts and delivery updates arrive on webhooks verified with `X-Twilio-Signature` over the exact address. The webhook answers with empty TwiML, so Twilio never texts an answer back. Turning on webhooks can set the address on the number or service. STOP and START (and Twilio's `OptOutType`) update the contact's opt-out in core. HELP is answered by Twilio and changes nothing. **Core**: a "Send a text" form on contact and matter pages works with either connector through the standard `send_text` action. Sent texts show their delivery status, which only moves forward. Delivery updates for texts the CRM never sent are ignored. **Framework**: the request address passed to `verify_webhook`, `register_webhook()`, credential-signed webhooks, per-connector acknowledgments, and optional settings. Setup guides: `connectors/dialpad/README.md`, `connectors/twilio/README.md`. |
| 3.5 Google Workspace (`connectors/google_workspace`, done) | Each staff member's own Gmail and Google Calendar (a shared mailbox is one more connection of the person who uses it). **Gmail**: the last 30 days, then Gmail history with paging; a 404 resyncs the last 7 days. Only headers and the snippet are read. Spam, drafts, Gmail's Promotions, Social, Updates and Forums tabs, bulk and automated mail, and email between staff are left out. Matched mail is stored on the contact. The rest goes to Needs matching, or is left out with the `unmatched` setting. A **Send an email** form sends plain text through the outbound policy (`send_email`); Gmail's sent copy lands on the same record. **Calendar**: events pulled with sync tokens (30 days back, then incremental; a 410 starts over) become appointments when a contact attends. Appointments assigned to the staff member are pushed with no attendees and `sendUpdates=none`, moved, and removed (`save_event` and `delete_event`). Only events the CRM created are changed (`ExternalRef.created_by_crm`); changes from the calendar are never echoed back. Near-duplicates within two minutes are merged. **Policy**: `owner_sync_actions` lets a personal connection run automatically only to keep its owner's own records in step. Setup guide: `connectors/google_workspace/README.md`, including the Internal app that avoids Google's verification for the restricted Gmail scope. |
| 3.6 Microsoft 365 (`connectors/microsoft_365`, done) | Outlook mail and calendar through Microsoft Graph, in one code path with one stored, rotating token. The firm's single-tenant app signs in at its own tenant's endpoint: OAuth `app_fields` fill `{tenant}` in the URLs. **Mail**: delta queries on the inbox and Sent Items (30 days, up to 5,000 messages, then changes). An expired delta link, which Graph reports with a 400-series error, starts again. Drafts, Outlook's Other mail, and email between staff are left out, as is unmatched mail if the `unmatched` setting says so. Email is sent as a draft and then sent, with immutable IDs, so the Sent Items copy lands on the same record. **Calendar**: a calendar-view delta from 30 days back to a year ahead, restarted every 30 days; appointments are pushed with no attendees through the same `save_event` and `delete_event` actions as Google. **Change notifications**: optional Graph subscriptions. The validation token is echoed through the new `webhook_challenge` hook and acknowledged with 202. Notifications are accepted only with the right `clientState` and a known subscription ID, and the worker then fetches what changed itself. Subscriptions last two days and are renewed or remade by the sync; delta polling every 10 minutes is the fallback. Disconnecting deletes them. Setup guide: `connectors/microsoft_365/README.md`. |
| 3.7 Calendly | An organization webhook, HMAC-signed with a timestamp window, for bookings, cancellations, and reschedules. A booking matches or creates the contact and creates the consult appointment. Which event types count as consults is set on the connection, not guessed from their names. UTM fields can fill a chosen lead-source custom field, which the operating dashboard groups by. |
| 3.8 LawPay | Payment links for an invoice's balance (outbound, through the policy). Charge webhooks, verified with the signing secret, record payments on the invoice they reference; refunds void them. Each LawPay account maps to operating or trust. The endpoints and signature header are checked against current AffiniPay documentation before writing code. |
| 3.9 AI models and AI settings | A provider-neutral AI interface: messages, function tools, and structured output. Connectors: OpenAI and Anthropic (API keys, plain HTTPS, OpenAI calls with `store: false`); OpenRouter (PKCE sign-in); and any OpenAI-compatible endpoint, which covers local models. An AI settings page where each feature is off until a superuser turns it on, and where the firm chooses which connection powers background work. Every request is logged with the connection that paid for it. Per-user connections run only that user's own interactive requests. |
| 3.10 AI features | Ported from the firm CRM with generic prompts. **Consult prep memos** for upcoming consults, built from CRM data. **Task suggestions** that staff accept or dismiss; they are read-only until accepted. **A matter assistant**: CRM read tools, and write tools that need confirmation for each action; drafts are never sent. All output is labelled as an AI draft for attorney review. Tool results are treated as untrusted data. Conversations are kept in the CRM, not at the provider. |
| 3.11 BIA Edge legal research | Authority search from a matter: query, results with citations and links, and saving selected authorities to the matter. Retries and a cold-start notice. |
| 3.12 ChatGPT plan (Beta, behind a flag) | As described under "AI access". It becomes Ready only after OpenAI confirms this use in writing. |

Ported, but changed:
- **Signatures.** Webhooks are verified in every environment. Dialpad's data comes from the
  verified token, and Twilio's inbound webhooks are signed and checked. The firm CRM accepted
  unsigned requests in debug mode, and its Twilio integration had no inbound webhook at all.
- **Texting.** Texts go through the outbound policy. Without credentials they fail rather than
  being marked sent.
- **Paging.** Calendar and mailbox syncs follow every page. Rate limits back off with `Retry-After`.
- **Tokens.** One token per connection, refreshed under a lock. The firm CRM stored Microsoft
  tokens twice.
- **Storage at providers.** OpenAI requests use `store: false`, and AI conversations are kept in
  the CRM.
- **Matching.** No last-7-digit phone matching. Unmatched records wait for staff.

Not in Phase 3:
- Google Drive storage moves to Phase 4, with OneDrive and SharePoint.
- Cover letter drafting is not ported. It depends on firm exemplars, and the firm CRM's exemplar
  fixtures contain real-looking client data.
- Google Ads, GA4, and website call tracking stay "Later" (marketing analytics and lead capture).
- The firm CRM's Obsidian vault sync is not ported, because it copies personal data out of the
  CRM.

Decided 2026-10-30:
- Order: the MCP server first (3.1), because it needs no provider and lets firms use their own
  Claude or ChatGPT plans at once. Then the framework (3.2), then the providers.
- New dependencies:
  - `django-oauth-toolkit` (3.4 or later) for the MCP authorization server, rather than writing
    OAuth endpoints by hand.
  - `httpx` for the shared client and its mock transport.
  - `phonenumbers` for international phone numbers.

  Provider SDKs stay out; connectors use plain HTTPS.
- MCP sign-in, checked while building 3.1. django-oauth-toolkit 3.4:
  - stores tokens only as hashes (`COMPLIANT_BCP_RFC9700_TOKEN_STORAGE`);
  - sends the RFC 9207 `iss`, which ChatGPT's preferred callback needs;
  - accepts S256-only PKCE.

  Hashed storage rules out a refresh-token grace period, so a reused refresh token revokes its
  chain. Dynamic registration stays available beside Client ID Metadata Documents. Claude Code's
  plain-http loopback callback is not accepted.
- Maturity: every ported connector ships as Beta. It becomes Ready once it has passed a live check
  against a real account and has a named maintainer.
- Live checks run by hand, on a non-production installation with the firm's own accounts. They
  never run in CI, and nothing recorded from a real account is committed.

## Testing

- Every capability has a contract test suite, and every connector runs it against recorded
  fixtures through the HTTP mock transport. CI never touches the network.
- Fixtures use fictional `.test` data and pass the privacy check.
- Negative tests: bad and missing signatures, replayed events, expired tokens, revoked scopes,
  and outbound attempts blocked by each policy step.
- An optional live sandbox suite runs by hand with each vendor's test credentials.
- Add `connectors` to the coverage source. The 80% floor applies.

## Porting notes from the firm CRM

That code is private to the firm and not in this repo. Port by rewriting against the interfaces,
not by copying whole modules. Review the firm's test fixtures for real client data before
anything is copied.

| Keep | Change |
|---|---|
| `EncryptedJSONField` with key rotation | The provider list is a hardcoded `TextChoices` → use a registry |
| `safety.py` outbound decisions: preview by default, kill switch, admin-only send | Webhook secrets are global settings → store one per connection |
| Normalized message and event dataclasses | Some webhooks are accepted unsigned in DEBUG → always fail closed |
| Graph `clientState` plus subscription-ID verification | Microsoft exists twice (a separate app and a provider adapter) → one framework only |
| Webhook payload data minimization | 2FA codes depend on an outbound email account → use TOTP |
| Dedupe and repair commands | Capability booleans on the account → a list of enabled capabilities |

## Decisions

Decided 2026-10-29:

1. **Hosted broker.** Design for it in Phase 1: the OAuth and webhook code must not assume the
   installation is always the OAuth client. Build it after Phase 4, and decide then whether it is
   a paid service. The tradeoffs stay the same. It would give one-click Google and Microsoft
   sign-in, but it would see tokens in transit, need Google's annual security assessment for
   restricted Gmail scopes, and be a service to operate.
2. **Repo layout.** First-party connectors live in this repo. Third-party connectors are
   installable packages.
3. **Django version.** Stay on 5.2 LTS with the `django-tasks` backport (see "Background jobs").
4. **Trust accounting.** Version 1 records which account a payment went to. No trust ledger.
5. **AI features.** Port consult prep memos, task suggestions and the assistant. Each is opt-in
   and off by default. See "AI access".
6. **Name.** "Connections".
7. **Third-party connector licensing.** The catalog lists only connectors under OSI-approved
   licenses compatible with AGPL-3.0.

## Open questions

1. **ChatGPT plan usage for this kind of app.** Ask OpenAI, through its Sign in with ChatGPT
   interest form:
   - Does a firm-hosted, multi-user, open-source web app qualify?
   - Can the callback be on the firm's own domain instead of `127.0.0.1`?
   - Are ChatGPT Business and Enterprise workspaces eligible?
   - What retention and confidentiality terms apply to plan-usage requests?

## Risks

- **Maintenance.** Every connector breaks when its vendor changes. Mitigation: Ready only with a
  maintainer and passing contract tests, and automatic demotion to Beta when either lapses.
- **Attack surface.** More credentials and more inbound endpoints. Mitigations: encryption,
  scope minimization, fail-closed webhooks and audit records.
- **Self-hosting friction.** BYO OAuth setup and public webhooks are real hurdles. Mitigations:
  wizards, polling fallbacks, and possibly the broker.
- **Trademarks.** Use names and logos only to identify compatibility, follow each vendor's brand
  guidelines and show the non-affiliation notice.
- **AI vendor policy.** Rules on using subscriptions in other apps changed several times during
  2026. Keep each AI path in its own connector, so one vendor's change cannot break the others,
  and recheck the rules before each release.
