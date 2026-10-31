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
   receives. Someone who manages connections and AI acknowledges that before it is enabled,
   and the acknowledgment is audited.
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
| System email | Staff copy intake links by hand | The CRM emails intake links and payment plan reminders | SMTP | Postmark, Amazon SES, SendGrid, Mailgun |
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
| `ConnectorApp` | Instance-level OAuth client ID and encrypted secret for "bring your own app" providers. Only people who manage connections and AI enter it. |
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
3. Preview mode (new connections start here; someone who manages connections and AI turns on
   live sending)
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

All AI features are opt-in and off by default. Someone who manages connections and AI turns on
each feature separately (consult prep memos, task suggestions, the assistant). Nothing goes to a
model until both the feature and an AI connection are on.

Firms can power AI in four ways. This reflects each vendor's published rules as of 2026-10-29:

| Path | Who pays | Scope | Usable by a self-hosted web app today |
|---|---|---|---|
| Bring the CRM into Claude or ChatGPT through the MCP server | The user's own Claude or ChatGPT plan | Per user | Yes. Claude custom connectors work on Free (one connector), Pro, Max, Team and Enterprise. ChatGPT custom MCP connectors are confirmed for Business, Enterprise and Edu on the web; Free, Plus and Pro are unconfirmed (checked 2026-10-30). |
| Sign in with ChatGPT, using plan usage | The user's ChatGPT plan | Per user | Built behind a flag (PR 3.12); not confirmed by OpenAI; see below |
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
  (create a task, log a note, save a draft) require confirmation, stay off until someone who
  manages connections and AI turns them on, and are audited on every call.
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
| 3. Port the firm's adapters, plus AI access (done) | Google, Microsoft 365, Dialpad, Twilio (texting), Calendly, LawPay, OpenAI, Anthropic, USCIS Case Status and BIA Edge, rewritten against the interfaces with firm-specific behavior removed. AI access: the MCP server, OpenRouter, the OpenAI-compatible endpoint, and the ChatGPT plan connector behind a Beta flag | Each connector passes its contract tests on recorded fixtures and has its data flows filled in. A staff member can work with the CRM from Claude through the MCP server. |
| 4. Second adapters (done) | IMAP/SMTP, Twilio voice, Dialpad texting, Stripe, Cal.com, Documenso, Zoom, CourtListener, Google Drive and OneDrive/SharePoint, the website-form webhook, OIDC sign-in and SMTP system email | No interface needed a vendor-specific escape hatch; any interface fixes were made in core and the first adapter was updated |
| 5. Ecosystem (done) | Connector SDK guide and template, entry-point packages, filled-out Wanted catalog, n8n/Zapier/Make recipes, migration importers, WhatsApp, shipping, translation; see "Phase 5 plan" | An outside contributor can ship a Beta connector without touching `crm/` |
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
| 2.5 REST API (`crm/api`, done) | `/api/v1/` for contacts, matters, work, notes, communications, time, notices, status checks, and invoices (read only), using the web forms' validation; tokens act as one staff member, are scoped per resource, hashed, expire within a year, and can be revoked; the API is off until someone who manages connections and AI turns it on; every request is logged | MCP server |
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
| 3.1 MCP server (`crm/mcp`, done) | Off until someone who manages connections and AI turns it on. **Endpoint**: `/mcp/`, written as plain Django views (the official SDK needs ASGI). It is stateless, answers with JSON only, checks `Origin`, and supports both protocol versions in use: 2026-07-28 (`server/discover`, version headers, no sessions) and the earlier version that Claude's docs still describe (`initialize`, `notifications/initialized`, `ping`). **Sign-in**: `django-oauth-toolkit` as the authorization server, which is tested for this role. It publishes authorization server metadata (RFC 8414) and protected resource metadata (RFC 9728), and returns a 401 with `WWW-Authenticate` to start sign-in. It accepts S256 PKCE only and binds tokens to the `resource` (RFC 8707), and refresh tokens rotate. Clients register through Client ID Metadata Documents (Claude's published identity and ChatGPT) or dynamic registration (Claude's "register automatically"), which is rate-limited. Each staff member approves access on a consent page after normal sign-in, including two-factor. **Tools** follow the API's scopes and the person's own permissions. Read tools: search contacts and matters, matter summary, deadlines, notices, communications, time, and invoices (with billing access). Write tools are off until someone who manages connections and AI turns them on: create a task, add a note, log a communication, record a status check. Every tool carries `title` and `readOnlyHint` or `destructiveHint`, so Claude and ChatGPT ask before writes. Every call is audited. Clients and tokens are listed on the API access page, where they can be revoked. **Setup guide** (`docs/MCP.md`): adding the connector in Claude and in ChatGPT. Reachability: `/mcp/`, `/o/`, and `/.well-known/` must be reachable from Claude (160.79.104.0/21) or ChatGPT (OpenAI's published list), and the consent page from staff browsers. ChatGPT's Secure MCP Tunnel does not carry the authorization server. |
| 3.2 Provider framework (done) | A shared HTTPS client (`httpx`): timeouts, retries that honour `Retry-After` on 429 and 5xx, response size limits, and a pagination helper. Tests use its mock transport with the network blocked. OAuth token refresh under a row lock before expiry; rotating refresh tokens are saved once; a refused refresh marks the connection "needs sign-in again". Typed records per capability (call, text, email, calendar event, booking, payment, case status) with a contract test suite each, replacing the free-form `Record.data`. Core handlers that store them: communications, appointments, payments, status checks. An `ExternalRef` table for records with no provider-ID field of their own, such as appointments. Matching: phone numbers stored in E.164 with a firm default country (`phonenumbers`) and emails lowercased, both indexed. Unmatched calls, texts, and emails go to a "Needs matching" list where staff link or create the contact. There is no partial-number matching. |
| 3.3 USCIS Case Status (`connectors/uscis_case_status`, done) | The firm's own developer.uscis.gov app (client credentials), with a sandbox or production setting. **Sandbox**: only USCIS's published test receipt numbers, once a day, which builds the days of traffic USCIS asks for before production access; client receipt numbers never go to the sandbox. **Production**: the worker checks the receipt numbers of open matters about once a day, two requests a second. A 429, or three receipt numbers in a row without an answer, stops the run, which keeps what it has. Each reading is recorded on its matter. A change creates a review task for staff, prioritized by the kind of change: no legal interpretation and no message to the client. Receipt numbers USCIS cannot answer for stay on the matter with USCIS's message, including the 8 U.S.C. 1367 caveat for "not found". **Framework additions**: connection settings (`SettingField`); `lookups.due()`, so connectors ask core what to check; `SyncBatch.warning`; and request spacing in the shared client. Setup guide: `connectors/uscis_case_status/README.md`. |
| 3.4 Phone and texting: Dialpad, Twilio (`connectors/dialpad`, `connectors/twilio`, done) | **Dialpad**: calls with recording links, voicemails, and Dialpad's AI recap arrive by webhook as HS256 JWTs, read only from the verified token. A call's separate events build one communication. An hourly sync catches up on calls from Dialpad's call list. Texts arrive both ways. Dialpad has no API for past texts, so only calls are backfilled. Turning on webhooks can create the Dialpad webhook (with a CRM secret) and company-wide call and text subscriptions; a new address or disconnecting deletes the old webhook. **Twilio**: texts only, from a number or Messaging Service, sent through the outbound policy. Incoming texts and delivery updates arrive on webhooks verified with `X-Twilio-Signature` over the exact address. The webhook answers with empty TwiML, so Twilio never texts an answer back. Turning on webhooks can set the address on the number or service. STOP and START (and Twilio's `OptOutType`) update the contact's opt-out in core. HELP is answered by Twilio and changes nothing. **Core**: a "Send a text" form on contact and matter pages works with either connector through the standard `send_text` action. Sent texts show their delivery status, which only moves forward. Delivery updates for texts the CRM never sent are ignored. **Framework**: the request address passed to `verify_webhook`, `register_webhook()`, credential-signed webhooks, per-connector acknowledgments, and optional settings. Setup guides: `connectors/dialpad/README.md`, `connectors/twilio/README.md`. |
| 3.5 Google Workspace (`connectors/google_workspace`, done) | Each staff member's own Gmail and Google Calendar (a shared mailbox is one more connection of the person who uses it). **Gmail**: the last 30 days, then Gmail history with paging; a 404 resyncs the last 7 days. Only headers and the snippet are read. Spam, drafts, Gmail's Promotions, Social, Updates and Forums tabs, bulk and automated mail, and email between staff are left out. Matched mail is stored on the contact. The rest goes to Needs matching, or is left out with the `unmatched` setting. A **Send an email** form sends plain text through the outbound policy (`send_email`); Gmail's sent copy lands on the same record. **Calendar**: events pulled with sync tokens (30 days back, then incremental; a 410 starts over) become appointments when a contact attends. Appointments assigned to the staff member are pushed with no attendees and `sendUpdates=none`, moved, and removed (`save_event` and `delete_event`). Only events the CRM created are changed (`ExternalRef.created_by_crm`); changes from the calendar are never echoed back. Near-duplicates within two minutes are merged. **Policy**: `owner_sync_actions` lets a personal connection run automatically only to keep its owner's own records in step. Setup guide: `connectors/google_workspace/README.md`, including the Internal app that avoids Google's verification for the restricted Gmail scope. |
| 3.6 Microsoft 365 (`connectors/microsoft_365`, done) | Outlook mail and calendar through Microsoft Graph, in one code path with one stored, rotating token. The firm's single-tenant app signs in at its own tenant's endpoint: OAuth `app_fields` fill `{tenant}` in the URLs. **Mail**: delta queries on the inbox and Sent Items (30 days, up to 5,000 messages, then changes). An expired delta link, which Graph reports with a 400-series error, starts again. Drafts, Outlook's Other mail, and email between staff are left out, as is unmatched mail if the `unmatched` setting says so. Email is sent as a draft and then sent, with immutable IDs, so the Sent Items copy lands on the same record. **Calendar**: a calendar-view delta from 30 days back to a year ahead, restarted every 30 days; appointments are pushed with no attendees through the same `save_event` and `delete_event` actions as Google. **Change notifications**: optional Graph subscriptions. The validation token is echoed through the new `webhook_challenge` hook and acknowledged with 202. Notifications are accepted only with the right `clientState` and a known subscription ID, and the worker then fetches what changed itself. Subscriptions last two days and are renewed or remade by the sync; delta polling every 10 minutes is the fallback. Disconnecting deletes them. Setup guide: `connectors/microsoft_365/README.md`. |
| 3.7 Calendly (`connectors/calendly`, done) | A personal access token from an organization admin. Turning on webhooks creates one organization webhook for `invitee.created` and `invitee.canceled`, signed with the CRM's secret (`Calendly-Webhook-Signature: t=…,v1=…`, HMAC-SHA256 of `t.body`) and refused outside a three-minute window; a new address or disconnecting deletes the old webhook. Which event types count as consults is chosen on the connection's page from Calendly's live list, never guessed from names, and other bookings are ignored. A booking matches or creates the contact (stage consultation) and makes the consult appointment, with the booking answers, location, and the staff member from the event. A reschedule moves the same appointment (`BookingRecord.replaces`); its paired cancellation is ignored. A real cancellation marks it cancelled. UTM values can fill a chosen contact choice field, such as the lead source the operating dashboard groups by, for new contacts only. **Framework**: settings with live `options` and `multiple` values, edited on the connection's page; `lookups.options()` for choices from core data. Setup guide: `connectors/calendly/README.md`. |
| 3.8 LawPay (`connectors/lawpay`, done) | Checked first against 8am's public developer documentation, as planned (AffiniPay is now 8am, at api.8am.com). That changed one part of the plan: **LawPay's webhooks are not signed**, and its docs say to treat a delivery as a hint and fetch the event, and recommend polling. So payments come in by polling the event list every 10 minutes, starting when the firm connects. An optional webhook is only a hint: the CRM reads the event ID, fetches the event from LawPay, and records only what LawPay returns. **Payment links** (`create_payment_link`, through the policy) are the firm's LawPay hosted payment pages, prefilled with amount, reference (the invoice number), name, and email, with amount and reference read-only. Creating links through the API needs a partner OAuth app, which a firm doesn't have. Core makes **one link per account still owed**, so advances go to trust and earned fees to operating, and keeps them on the invoice (`PaymentLink`). A completed charge with an invoice number as its reference is recorded once to the account it was paid into; a whole refund voids it, and a partial refund (`partially_refunded`) is noted for staff. Accounts map to trust or operating from the connection's settings, then LawPay's own trust flag; any other account waits for staff. Setup guide: `connectors/lawpay/README.md`. |
| 3.9 AI models and AI settings (`crm/ai`, done) | **Core**: `generate()` takes messages, function tools, and a JSON schema for structured output. Replies carry the provider's own copy of the assistant's turn, which is sent back unchanged in tool loops. Every request goes through the outbound policy (preview mode, the hourly limit, and personal connections running only their owner's interactive requests). Each is logged in `AIRequest` with the feature, person, connection, model, and tokens, but no prompts or answers. **AI features page**: AI and each registered feature are off until someone who manages connections and AI turns them on; that person chooses the firm-wide connection for background work and can send a test message with no client data. **Connectors**, each checked against current documentation: Anthropic (Console API keys only, with Claude.ai subscription credentials refused; API version `2026-10-21` with no sampling parameters; `output_config.format`; default `claude-opus-5-5`). OpenAI (Responses API with `store: false`, output items echoed back in tool loops, strict `text.format`). OpenRouter (PKCE sign-in returning a key, through the new `KeyExchangeAuth`; `data_collection: deny`, or zero data retention, on every request). OpenAI-compatible endpoints, including local models (plain HTTP only to this computer or the firm's network; the key is optional). Guide: `docs/AI.md`. |
| 3.10 AI features (`crm/ai`, done) | Ported from the firm CRM with generic prompts; the CRM records each feature sends are built in one module. **Consult prep memos**: the worker drafts one for each consult in the next two days through the background connection, and drafts it again only when its records change; staff can ask for one through their own choice of connection. **Task suggestions**: the worker reviews new communications hourly, grouped by matter or contact, and skips anything much like open work or a recent or dismissed suggestion. Accepting opens the new-task form filled in; nothing is created before that. **Matter assistant**: the MCP server's read tools run as the staff member; write tools wait for confirmation of each action; a draft tool shows emails and texts in the send form but cannot send. Tool results go back marked as untrusted data. Answers run in the worker, which now also queues periodic work through a `worker_ticked` signal. All output is labelled as an AI draft for attorney review. Conversations are kept in the CRM; a provider's own copy of a turn is sent back only to that provider. |
| 3.11 BIA Edge legal research (`crm/research`, done) | **Core**: a search from a matter goes through the outbound policy as the `search_authorities` action. Each search and its results are kept (the latest 25 per matter). Staff save results to the matter with a note, kept once per matter and source, or add authorities by hand when no research service is connected. A connector lists what it can search in `search_scopes`. Saved authorities appear in the matter summary that the MCP server and the assistant read. **BIA Edge**: its REST search API has been retired, so the connector calls the MCP server's search tools, `search_cases`, `search_unpublished_cases`, `search_statutes`, and `search_references`, with the firm's `bia_live_` key as a bearer token. The server is stateless, so each search is one request. Results come from the structured records (schema 2.0), and only the source links BIA Edge returns are shown. HTTP failures are retried once. A busy or rate-limited answer is tried once more after the wait BIA Edge gives. A slow answer gets a clear try-again message. The cold-start notice was dropped, since BIA Edge no longer sleeps between uses. A new `crm/connect/mcp_client.py` serves any MCP-based connector. |
| 3.12 ChatGPT plan (Beta, behind a flag; done) | Built to OpenAI's Sign in with ChatGPT documentation (read 2026-10-30). **Flag**: connectors can declare a `flag`, offered only while `CONNECTOR_FLAGS` lists it; turning it off stops their connections, and the catalog shows the provider as Wanted again. **Sign-in**: OpenAI allows only a loopback callback, so `KeyExchangeAuth` gained `loopback_redirect`: staff open **Continue with ChatGPT** in a new tab, land on an address that cannot load, and paste it back. The first sign-in registers dynamically; the issued client ID, per user and workspace, is kept for later sign-ins with `id_token_hint`. The connection ID serves as the host ID. The ID token is checked against OpenAI's keys (issuer, audience, expiry, nonce), and the grant must include `chatgpt.tokens.use.direct`. A new `refresh_credentials` hook refreshes under the row lock, so a rotating refresh token is never spent twice. Ended sign-ins ask staff to sign in again, and disconnecting revokes the refresh token. **Requests**: Responses API with `store: false` and `stream: true`, read to `response.completed`. Function tools go in a `crm` namespace, and unsupported fields are left out. Plan-usage error codes get plain messages, and the limit message links to Manage usage. **Labels**: connectors can declare `usage_label` and `usage_url`, shown as **Using ChatGPT plan** with a **Manage usage** link wherever staff choose an AI connection. **Still open**: OpenAI's written confirmation and data terms, the ChatGPT logo (not published), and per-server revocation, which OpenAI does not offer yet. |

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

## Phase 4 plan

Phase 4 adds a second provider for each capability that has one, and a first provider for the
capabilities that work only by hand so far. It is done when no connector needed a
vendor-specific escape hatch: any gap a second provider exposes is fixed in core, and the first
connector is updated to match. Each provider is checked against its current documentation when
its PR is built, and ships as Beta.

Dialpad texting, listed for this phase, shipped in 3.4.

| PR | Scope |
|---|---|
| 4.1 CourtListener legal research (`connectors/courtlistener`, done) | Second legal research connector, against the research interface from 3.11, checked against CourtListener's v4 documentation on 2026-10-30. The research interface needed no change. **Personal connections**: CourtListener's terms forbid sharing or pooling a token, so each staff member connects their own account and it searches only for them. **Search**: case law (`type=o`), highlighted, by relevance, with published and unpublished opinions both included. Scopes: the federal courts of appeals and the Supreme Court (the default), the Board of Immigration Appeals, the Supreme Court alone, or every court. Links go to CourtListener's opinion pages. **Limits**: the default account allows 5 searches a minute, 50 an hour, and 125 a day, so a limited search is not retried, and staff are told when to try again. The token is checked through the usage endpoint, which does not count against those limits. **Contract tests**: every research connector's results are now checked for core's shape. |
| 4.2 Cal.com scheduling (`connectors/calcom`, done) | Second scheduling connector, beside Calendly, checked against Cal.com's API v2 and webhook documentation on 2026-10-30. The scheduling interface and `BookingRecord` needed no change. **Setup**: a `cal_` API key; for a self-hosted Cal.com, the address of its API v2 service (plain HTTP only on the firm's network). The consultation event types are picked from Cal.com's live list. Turning on webhooks creates one account webhook for created, rescheduled, and cancelled bookings, signed with the CRM's secret. Team event types need the same address added by hand, which the connection page explains. **Checks**: `X-Cal-Signature-256` is a hex HMAC of the body with no signed timestamp, so deliveries also need a `createdAt` within an hour, and each event is filed once. Unsigned deliveries are refused, and Cal.com's ping is accepted. **Records**: a reschedule names the booking it replaces (`rescheduleUid`), so the appointment moves. Hidden booking questions are left out, and Cal.com's stand-in `@sms.cal.com` addresses are not kept. Cal.com sends no tracking values, so lead source is not filled. Cal.com does not retry failed deliveries, which the guide notes. |
| 4.3 Stripe payments (`connectors/stripe`, done) | Second payments connector, beside LawPay, checked against Stripe's API documentation (version `2026-10-28.endive`) on 2026-10-30. **Two gaps fixed in core**: a payments connector now says which accounts it takes payments into (`deposit_accounts()`), and the invoice page asks it for links into those only, saying when part of an invoice needs another connection; and a provider-issued webhook secret can be stored by `register_webhook()` instead of being pasted. **Key**: a restricted key only (`rk_`), with the permissions the setup guide lists; secret keys are refused. **Accounts**: one Stripe account pays out to one bank account, so each connection is set to operating (the default) or trust, with a warning that Stripe takes its fees from each payment. **Links**: one Payment Link per invoice account, with an inline price, the invoice number and account in its metadata and on the payment, the client's email filled in, and payment methods left to the firm's Stripe settings. The connector deactivates a paid link and reactivates it if a bank payment fails. **Payments**: signed webhooks (`Stripe-Signature`, five-minute tolerance) for checkout completion, bank payments clearing or failing, and refunds. A full refund voids the payment. Payments not made through the CRM's links are ignored and not kept. The CRM can create the webhook endpoint and keep Stripe's secret, or staff can paste one. |
| 4.4 IMAP/SMTP mailbox (`connectors/imap_smtp`, done) | Third mailbox connector, for any mail provider, with Python's own IMAP and SMTP libraries. The mailbox interface, `EmailRecord`, and the send path needed no change. **Connecting**: each staff member's own mailbox, with their address and an app password and the provider's server names. Both connections always use TLS with certificate checks (SMTP over SSL or STARTTLS). **Reading**: the inbox and the sent folder, found from the server's Sent flag or a usual name, or set by hand. The first sync looks back 30 days, then each folder continues from its last UID, looking back 7 days if the server renumbers it (UIDVALIDITY). Messages are read without marking them read. Headers plus an excerpt from messages under 256 KB are kept; attachments are not. Bulk mail is skipped, and the unmatched-mail choice matches Google's and Microsoft's. **One record per email**: messages are filed under their Message-ID, which is also what sending returns, so a sent email and its sent-folder copy meet. **Sending**: plain text by SMTP, with a copy put in the sent folder unless the provider keeps one; a lost copy does not undo the send. |
| 4.5 Twilio voice (`connectors/twilio`, done) | Second phone connector, beside Dialpad, added to the existing Twilio connector as its Phone capability, and checked against Twilio's Call and Recording documentation on 2026-10-30. The phone interface and `CallRecord` needed no change. **Polling, not callbacks**: status callbacks would mean changing each number's or each TwiML's setup, and Twilio's account-wide call events come from Voice Insights. So the CRM reads the call log every 15 minutes and never changes call routing. The first sync looks back a week, then later ones go back two hours from the last call, since Twilio filters by day; `next_page_uri` is followed. **Legs**: a forwarded or browser-placed call is a parent and a child leg, grouped by parent and filed once, with the outside party from whichever leg has it. A call is filed only once every leg has ended. The outcome is answered, missed, or no answer. **Recordings**: Twilio can require credentials to play recording media, so recordings are noted by ID and length with where to find them, not linked. **Numbers**: the firm can choose which of the account's numbers to log calls for. Texting's sender is now needed only when texting is on. The data flows moved to version 2, so existing Twilio connections are reviewed again. |
| 4.6 Website-form webhook (`connectors/website_form`, done) | First lead-capture connector. **Core**: a new `LeadRecord`, and `crm/leads.py`, which matches a lead by email or phone, or makes a contact at the Inquiry stage with the lead source filled as for bookings, and keeps each submission as an intake note; an existing contact is never otherwise changed. **Signing**: the Standard Webhooks scheme the outbound webhooks already use, with a `whsec_` secret made at connect time and shown once. A submission that is unsigned, wrongly signed, or more than five minutes old is refused, and each `webhook-id` is filed once, so captured submissions cannot be replayed. **Body**: JSON with a name, an email or phone, and optionally a message, the form's name, `source` and `utm_` values, and answers, nested or at the top level. Unusable bodies get a bare 400. The guide has signing examples, and says signing belongs on the site's server or in a tool such as n8n, never in the browser. |
| 4.7 SMTP system email (`connectors/smtp`, done) | First system email connector, checked against the SMTP documentation of Postmark, Amazon SES, SendGrid, Mailgun, Microsoft 365, and Google Workspace on 2026-10-30. **Core**: a `send_system_email` action and `crm/comms/system_email.py`, which sends through the firm's oldest active system email connection via the outbound policy and logs each sent email on the contact with a summary the caller writes. **Intake links**: staff can have a new link emailed to the contact; the page still shows it once to copy, and says why if the email was not sent. The link is never stored or logged. **Payment plan reminders**: the deferred automated reminders from 2.3. A plan can email the client at 9 a.m. on each installment's reminder day, chosen when the plan is made or later; each installment is claimed once by the worker, nothing goes out once it is paid or past due, and an email that was not sent is noted on the staff reminder. **Connector**: one connector for any SMTP server or relay, since all six services accept SMTP. STARTTLS on 587 by default (Postmark has no 465), or SSL on 465, always with the certificate checked, through the TLS code the IMAP/SMTP connector now shares. Plain text from the firm's verified address and name, an optional Reply-To, and `Auto-Submitted: auto-generated`. **Catalog**: a wanted provider can name the installed connector it already works through (`works_through`), so Postmark, Amazon SES, SendGrid, and Mailgun read "Works through SMTP server" and Zoho Mail reads "Works through IMAP and SMTP email", while staying Wanted for a connector of their own. **Not emailed**: two-factor backup codes, listed in the catalog before; emailing them would let anyone who reads the mailbox past the second factor. Setup guide: `connectors/smtp/README.md`. |
| 4.8 OIDC sign-in (`connectors/oidc`, done) | First sign-in connector, checked against OpenID Connect Core and Discovery, Google's OpenID Connect guide, and Microsoft's ID token and optional claims references on 2026-10-30. **Core**: `crm/connect/sign_in.py` and `SignInLink`. A firm-wide connection with the Sign-in capability, once its data flows are reviewed, adds a button to the sign-in page. The flow is the authorization code flow with a one-time state, nonce, and S256 PKCE verifier, good for 10 minutes. Only existing, active staff accounts sign in: the first time by an email the provider vouches for, matching exactly one account, then by issuer and subject alone. No account is created. The CRM's own two-factor step still follows, and password sign-in is unchanged. Superusers see and delete links in the admin. **Contract tests**: sign-in connectors must put state, nonce, and the code challenge in the address, prove an identity, and refuse a different nonce. **Connector**: one generic OpenID Connect client with presets. Settings come from the discovery document, whose issuer must match. The firm's client secret is sent as the provider lists (`client_secret_post` or `client_secret_basic`). The ID token's signature is checked against the provider's keys even though it arrives over TLS, along with issuer, audience (one only), `azp`, expiry, issue time, and nonce. Every sign-in must be from one of the firm's email domains. **Google**: either issuer form, and the `hd` claim must be the firm's Workspace domain (the `hd` request parameter is only a hint). **Microsoft Entra ID**: a single tenant by its ID; identities are the object ID (`oid`) in that tenant, since `sub` changes with the app registration. Guests (`acct` 1) and other tenants are refused, and an email is trusted only with the `xms_edov` optional claim, as Microsoft advises against trusting `email` alone. **Second factor**: skipped in the CRM only when the firm chose it and the ID token's `amr` includes `mfa`; Google usually does not send `amr`. **Connection check**: the discovery document, and a made-up code to the token endpoint, which tells a refused client (`invalid_client`) from a refused code. **Catalog**: Okta and JumpCloud point to this connector (`works_through`, from 4.7). Setup guide: `connectors/oidc/README.md`. |
| 4.9 Zoom video meetings (`connectors/zoom`, done) | First video connector, checked against Zoom's OAuth, granular scopes, Meetings API, and rate limit documentation on 2026-10-30. **Core**: `WorkItem.meeting_url` (the by-hand path: staff paste any meeting link, HTTPS only, also through the API), and `crm/meetings.py`, which keeps one meeting per appointment on the assigned staff member's own video connection through `save_meeting` and `delete_meeting` (owner-sync actions, so only for their own appointments). Only the title and time are sent, and the meeting is updated only when they change (a fingerprint on the `ExternalRef`). It is deleted when the appointment is deleted, unscheduled, or reassigned; a finished appointment keeps it; a pasted link is never replaced. The join link also goes in the appointment's calendar event description. **One gap fixed in core**: `OAuthAuth.token_auth`, so a provider that wants the app's ID and secret in an HTTP Basic header, as Zoom does, gets them there for the code exchange and refreshes. **Connector**: a General, user-managed Zoom app the firm keeps unpublished (it then works only for the firm's own Zoom account, and Zoom sends it no deauthorization notices); OAuth with S256 PKCE and the granular `meeting:read/write/update/delete:meeting` scopes; rotating refresh tokens saved once. Scheduled meetings (type 2) with the time in UTC and the firm's time zone; settings are left to the staff member's Zoom defaults. Updates are PATCH then GET, and a meeting deleted in Zoom (404, code 3001) is made again. Deletes send no reminder emails. Only the meeting ID and join link come back; the host's `start_url` is never kept. Zoom's limit of 100 meeting creations or updates a day per person gets a plain message, not retries. Disconnecting revokes the access token. Setup guide: `connectors/zoom/README.md`. |
| 4.10 Documenso e-signature (`connectors/documenso`, done) | First e-signature connector, checked against Documenso's API v2 documentation and source on 2026-10-30. **Core**: a new `crm/signatures` app. From a matter's **Signatures** panel, staff send one of its PDFs to the matter's contacts that have an email (`send_for_signature`, through the outbound policy), and cancel an open request (`cancel_signature`). A `SignatureRequest` keeps the provider's ID, the signers, and the status. A new `SignatureRecord` reports a signer signing, everyone signing (with the signed PDF, filed on the matter as a new document), declining, or cancelling. Reports for documents the CRM did not send, and any after a request is finished, are ignored. `connectors/example_esignature` is a fictional service for core tests. **Connector**: hosted or self-hosted (plain HTTP only on the firm's network), with a team API token in the raw `Authorization` header. **API v2 envelopes**, since API v1 ends on 2027-03-01: one multipart `envelope/create` with the PDF, the signers, and a signature field each at the bottom of the last page (page count read from the PDF), then `envelope/distribute`, so Documenso emails the signers; cancelling uses `envelope/cancel`. **Webhooks**: Documenso sends the secret itself in `X-Documenso-Secret` rather than signing, so a delivery is checked in constant time and then trusted only for the envelope ID. The worker reads the envelope's status from the API and downloads the signed PDF (`version=signed`), which must be a PDF. Webhook setup is in Documenso's team settings only. Setup guide: `connectors/documenso/README.md`. |
| 4.11 Google Drive storage (`connectors/google_drive`, done) | First document storage connector, checked against Google's Drive API, scopes, uploads, and quota documentation on 2026-10-30. **Core**: `crm/document_storage.py`, against three actions, so OneDrive and SharePoint can follow (4.12): `save_folder` (make or rename the matter's folder inside the folder the firm chose), `save_file`, and `trash_item`. A new document is copied when added, a renamed matter renames its folder, and deleting a document or matter moves its copy to the trash. Copies are linked through `ExternalRef`; a run every 15 minutes, recorded as a Document storage `SyncRun`, copies up to 25 documents with no copy yet, so documents from before the connection and failed copies catch up. Nothing is read back. `connectors/example_storage` is a fictional drive for core tests. **Connector**: a firm-wide connection through the firm's own Google Cloud app, with the folder chosen by its link. The `drive` scope, because `drive.file` cannot reach a folder the firm chose without Google's Picker scripts, which the CSP does not allow; an Internal app needs no verification, and the guide recommends a dedicated account that can see only the firm's shared drive. `supportsAllDrives` on every request. The connection check requires a folder in use where the account can add files and move them to the trash (Content manager on a shared drive). Folders and files carry the CRM's ID in private `appProperties`. Uploads are resumable; a folder deleted in Drive is made again; trashing something already gone counts as done. Disconnecting revokes the token. Setup guide: `connectors/google_drive/README.md`. |
| 4.12 OneDrive and SharePoint storage (`connectors/onedrive_sharepoint`, done) | Second document storage connector, checked against Microsoft Graph's drive item, upload, sharing, permission, and throttling documentation on 2026-10-30. The storage interface from 4.11 needed no change. **Connector**: firm-wide, through the firm's single-tenant Entra app (the tenant fills the sign-in URLs, as for Microsoft 365), with the delegated `Files.ReadWrite.All` and `offline_access` permissions, since `Files.ReadWrite` reaches only the person's own OneDrive and the Selected permissions need an admin grant through Graph; the guide recommends a dedicated account that is a member of only the client-documents site. The folder is chosen by its **Copy link** link, resolved through `/shares` without redeeming it. Folders and files are named `"{drive ID}:{item ID}"`, which renames and moves do not change. A taken name gets a number (`conflictBehavior=rename`, in the body and the URL for folders), and names SharePoint refuses are adjusted. Files up to 4 MiB are uploaded with one PUT; larger ones through an upload session in 3 MiB chunks (a multiple of 320 KiB) sent without the Authorization header. Deleting moves the copy to the recycle bin; `permanentDelete` is never used. SharePoint's recommended `NONISV|` user agent is sent. Graph has no write-permission check, so a folder the account cannot write to shows as a failed copy on the connection's page. Setup guide: `connectors/onedrive_sharepoint/README.md`. |

Decided 2026-10-30:
- Order: second providers for interfaces that already exist come first, smallest first, while
  those interfaces are fresh. Capabilities with only a manual path follow, and storage comes last
  because it is the largest.
- Each PR adds a contract test for its capability's interface where one is missing, so the
  next provider of that capability is held to the same rules.

Outcome (2026-10-31): Phase 4 is done. No connector needed a vendor-specific escape hatch; each
gap a provider exposed was fixed in core, for every provider of that capability:
- Payments: a connector says which accounts it takes payments into (`deposit_accounts()`), and a
  provider-issued webhook secret can be stored by `register_webhook()` (4.3).
- OAuth: `OAuthAuth.token_auth`, for providers that want the app's ID and secret in an HTTP
  Basic header (4.9).
- Catalog: `works_through`, so a wanted provider can point to the connector that already serves
  it (4.7).
- The connection page lists every connection's scheduled runs, and warns of a stopped worker for
  any connection whose work runs there (4.11).
- New core features with their manual paths and contract tests: leads (4.6), system email (4.7),
  staff sign-in (4.8), video meetings (4.9), e-signature (4.10), and document storage (4.11),
  which a second provider then used unchanged (4.12).

## Phase 5 plan

Phase 5 opens the CRM to connectors built outside this repository, fills in the manual paths
still missing from core, and adds the providers the catalog lists for migration, translation,
shipping, and WhatsApp. It is done when an outside contributor can ship a Beta connector as
their own package without touching `crm/`. As in Phase 4, each provider is checked against its
current documentation when its PR is built and ships as Beta, and live checks stay by hand.

| PR | Scope |
|---|---|
| 5.1 Connector SDK (done) | The path for connectors built outside this repository. A guide (`docs/CONNECTOR_SDK.md`): what a connector package may import (`crm.connect.base`, `records`, `http`, `testing`, `registry`), the manifest, the contract tests, and the `open_immigration_crm.connectors` entry point. A connector API version, checked when the registry loads a package, so an installation refuses a package written for a newer interface instead of failing later. `manage.py startconnector` writes a package skeleton for one capability: `pyproject.toml` with the entry point, the connector with its manifest, a contract test with a fake transport, and a README. CI installs a sample package from `examples/` and checks that the registry loads it through the entry point. |
| 5.2 Wanted catalog (done) | All 61 wanted entries have a one-line description of what the connector would do, a link to the provider's developer documentation, and how its API is opened up (`api_access`: open to any account, approval of an app, partners only, or no public API), all checked on 2026-10-30. Headnote alone has no developer documentation to link. Entries for providers that now have connectors (IMAP and SMTP email, Stripe, BIA Edge, CourtListener, the website form, and a second Cal.com) were removed, and a test keeps installed connectors out of the catalog, apart from a flagged one such as ChatGPT plan, which shows as wanted while its flag is off. **Works through**: WordPress forms through the website form connector; Azure OpenAI and Gemini through the OpenAI-compatible endpoint connector, which both document; Okta and JumpCloud through OpenID Connect. Amazon Bedrock and Vertex AI were left out: Bedrock's endpoint has no model list for the connection check, and Vertex AI takes an hour-long OAuth token rather than a key. A wanted provider's page has a **Building it** section with the documentation link, the API access, and `startconnector` and the SDK guide. |
| 5.3 Automation recipes (done) | `docs/recipes/`: two recipes, each as an n8n workflow to import and as steps for Zapier and for Make, checked against those tools' documentation on 2026-10-30. **Task when a USCIS status changes** checks the event's Standard Webhooks signature against the raw body, refuses one more than five minutes old, answers, and adds a task on the matter through the API with a `work:write` token. **Website form leads** signs each submission with the Website form connection's secret and sends the signed bytes unchanged. The guides say where each tool keeps client data and secrets. Tests (`connectors/webhooks/test_recipes.py`) run the n8n Code nodes and Zapier code steps in Node against events the webhook connector signs, send what they build to the real endpoints, and check every event type, API path, and link the pages name. Make's formulas are not run, and no recipe has been run in the hosted tools themselves. |
| 5.4 Spreadsheet import (done) | `crm/imports/`, for staff whose role includes Import ([guide](SPREADSHEET_IMPORT.md)). **One file, one template**: each row is a contact, with a matter when it has one, so a migration's usual export of matters with their clients imports in one pass. **Columns**: matched to fields from their headers, including the firm's custom fields; staff check each match, say how dates are written, and choose defaults. **Rows** are checked with the contact and matter forms. Choices match by name or value, dates need a four-digit year, and Assigned to takes a staff email or username. **Matching**: a row whose email or phone matches an existing contact, or a row earlier in the file, never changes that contact; its matter is added to it, and a row with no matter is skipped. So are rows matching two contacts and matters whose receipt number is already known. **Nothing is saved until staff confirm**, then every accepted row is saved in one transaction. **Report**: each skipped row's number and reason, which never repeats the row's values, and a download of the skipped rows as they were, to fix and re-import. **Retention**: the file is stored encrypted for a day, and the worker deletes it with any import never confirmed. The migration entries in the catalog point to the import. |
| 5.5 Clio Manage importer (done) | Built to Clio's API v4 documentation (read 2026-10-30). **Migration interface**: `ContactRecord` and `MatterRecord` (a matter names its client and other parties by their records' IDs). A connector with `sync_interval = None` syncs only when staff start it, with the new **Fetch records to import** (generally, **Sync now**) on the connection's page. Core (`crm/imports/migration.py`) turns the records into a 5.4 import, leaving out matters the connection imported before. The import keeps each row's source IDs, encrypted and deleted with the file. Confirming links each saved record through `ExternalRef`, links contacts already in the CRM rather than skipping them, and adds a matter's other contacts as parties. A client's status and stage follow its matters. **Connector** (`connectors/clio/`): the firm's private app, in one of four regions (`app.clio.com`, `ca.`, `eu.`, `au.`), chosen from a list. App fields can now be a list, and `Connector.app_setting()` reads them. Contacts, matters, and relationships are read with cursor paging in ID order, at most 200 a page, spaced for Clio's 50 requests a minute; a next-page link to any other host is refused. Companies are reported, not imported, because the CRM's contacts are people. Practice areas suggest a case type, and a responsible attorney who is active staff is assigned. Disconnecting deauthorizes the token. **Not checked live**: Clio documents its sign-in addresses for the US region only. **Terms**: registering a Clio app means accepting Clio's Developer Terms, which reportedly restrict competitive use; the setup guide asks firms to read them. |
| 5.6 Translation (done) | **Core** (`crm/translations/`): a **Translate** form under each communication on contact and matter pages, and on intake submissions, into a language staff choose from 14. It works through a new `translate` action, `{"texts": [...], "language": code}`, and the outbound policy, whose record names the language, never the text. A `Translation` is kept beside the original, never instead of it: the translated values by field or question, the source language, and the translator, credited by the service's name as DeepL's terms require. It is labelled as a machine translation to check against the original. The newest translation into a language replaces the last. **AI**: a Translation feature, off by default, lets staff translate through an AI connection they may use, with instructions to treat the text as data and keep names, numbers, and dates as written. **DeepL** (`connectors/deepl/`), built to its documentation (read 2026-10-30), with a **Pro key only**: DeepL's API Free terms forbid personal data, so a Free key (`:fx`) is refused before anything is sent. Under Pro, texts are deleted after translation and not used for training. Texts go in batches of at most 50 and 100 KB, below DeepL's limits, with a fixed context note. Quota (456) and key (403) errors get plain messages. Haitian Creole is refused, since DeepL has no such target; AI translation can do it. `connectors/example_translation/` is a fictional service for core tests. |
| 5.7 Shipments and USPS tracking (done) | **Core** (`crm/shipments/`): a **Shipments** panel on each matter, where staff record what was sent, the carrier, the tracking number, where it went, and when. They can update it by hand, mark it delivered or returned, and switch off automatic checks. Shipping connectors get the tracking numbers due through `lookups.due("shipping", carrier=...)`, which now passes filters: shipments not delivered or returned, sent in the last 60 days, and not checked in four hours. They return `ShipmentRecord`s with a status in the CRM's terms, or a problem. A delivery is noted on the matter; a problem or a return is noted with a high-priority task for the matter's owner. A package the carrier will not share (`not_shared`) is no longer checked. **USPS** (`connectors/usps/`), built to the Tracking API v3r2 documentation (read 2026-10-30): the firm's app with the client credentials grant, 35 numbers a request, and USPS's status categories, old and new, mapped with the latest event code, so a return to sender is told from a delivery. **Limit found**: since April 2026 USPS shares tracking only for packages labelled under the firm's own Mailer ID, so counter labels are likely not tracked. The setup guide says so first, and those shipments are kept by hand. |
| 5.8 UPS and FedEx tracking (done) | Second and third shipping connectors, against the interface from 5.7 unchanged, each built to its documentation (read 2026-10-31). **UPS** (`connectors/ups/`): the firm's app, tied to its shipper account, with the client credentials grant (HTTP Basic). One tracking number per request, as the API requires, spaced a second apart and at most 40 a run, inside the free tier's monthly quota. Status comes from UPS's activity type and its zero-padded status codes (out-for-delivery and return-to-sender codes). **FedEx** (`connectors/fedex/`): the firm's project, linked to its account, with a cached one-hour token, as FedEx asks. Up to 30 numbers a request, with FedEx's status codes (unknown codes taken as in transit), the actual and estimated delivery times, and exception reasons. Authenticated Delivery packages count as not shared. **Core change**: UPS's agreement limits keeping its data to nine months, so notes and tasks now use the CRM's words, never a carrier's, and the worker clears a carrier's own words from a shipment nine months after its last check. Status and delivery date are kept. **For a legal read**: UPS's agreement on third-party platforms and distribution, and FedEx's split between shippers and integrator providers; the setup guides say each firm connects its own app for its own packages. Shipping leaves the wanted catalog. |
| 5.9 WhatsApp Business (done) | A third texting connector, built to Meta's Cloud API documentation (read 2026-10-31), through the firm's own Meta app, WhatsApp Business Account, and number, with a system user's token. **Core**: a connector can declare a `reply_window`, here WhatsApp's 24-hour customer service window. Free-form texts go only within that time of the contact's latest message to the connection. Outside it, the contact page links to a page of the service's approved templates (`message_templates()`, kept five minutes since Meta limits how often they are read). Staff fill in each template's values and send it with the new `send_template` action. The sent message is logged as the template's text with the values in place. The webhook address now answers a provider's GET check through `webhook_challenge()`, and `webhook_details()` shows values such as a verify token beside the address. A delivery update without a number no longer clears the stored one. **WhatsApp** (`connectors/whatsapp_business/`): text and template messages, numbered or named values, and templates that need a photo, a link value, or a code left out. Incoming messages and delivery updates arrive batched, signed with the app secret (`X-Hub-Signature-256`), and are kept only for the firm's own number. Attachments are noted, not downloaded. A client with a WhatsApp username may arrive without a number and waits in Needs matching. STOP replies opt the contact out of all texting, as for texts. Meta's errors are explained by code. **Findings**: business verification is not needed to start (new portfolios reach 250 people a day). WhatsApp does not deliver marketing templates to US numbers. Free-form replies have been charged since October 1, 2026, after 1,000 a month per number. **For a legal read**: Meta's terms disclaim fitness for legal services and ask that data the law protects not be given to Meta. Cloud API reads messages and keeps them for up to 30 days. The Platform Terms bar eligibility decisions on immigration status from platform data. Sharing with third parties, including AI and translation services, is limited. Deletion requests must be squared with file retention. The setup guide lists these for the firm's lawyer. WhatsApp leaves the wanted catalog. |
| 5.10 Attachments on messages (done) | A follow-up to 5.9. **Core**: a `TextRecord` can carry `attachments`. Core keeps them with the message as `CommunicationAttachment`s, on the same terms as a document upload: a PDF, Word document, PNG, or JPEG by its contents, under the upload limit, the first time the message is seen. It notes on the message any it does not keep, and deletes the files with the message. Staff download a file from the message, as an attachment and never shown in the browser, or save a copy to one of the contact's matters as a document, noted on the matter. **WhatsApp**: the worker fetches photos and PDF or Word documents from Meta, which keeps them for 7 days, sending the access token only to Meta's own hosts. Voice messages, videos, stickers, other files, larger files, and files that could not be fetched are noted instead. The data flows moved to version 2. |
| 5.11 Sharing limits (done) | A follow-up to 5.9. **Core**: a connector can declare `sharing_limits`, a short note of the provider's terms on sharing what it brings in. The communications its connections bring in are then left out of the MCP server and the matter assistant's tools, task suggestions, consult prep memos, and translation. Each message shows the note where translation would be offered. Someone who manages connections and AI can allow it for one connection under **AI and translation** on its page, recorded in the audit log (`Connection.ai_use_allowed`). The REST API and outbound webhooks are unchanged. **WhatsApp** declares it, since Meta's terms limit sharing WhatsApp data with third parties. |

Decided 2026-10-31:
- Order: the contributor path first (5.1 to 5.3), since it is what Phase 5 is measured by. Then
  each new capability's manual path in core before its first connector (5.4 before 5.5, 5.6 and
  5.7 with theirs), and WhatsApp last.
- Importers for other systems follow 5.5 where an API exists and a firm with access can check
  one: Docketwise and MyCase publish APIs that need the vendor's approval, INSZoom offers one to
  subscribing firms, and LawLogix publishes none (checked 2026-10-30). None documents an export
  format, so until then their catalog entries say to use the spreadsheet import (5.4).
- Engagements stay deferred, and the hosted broker stays Phase 6.

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
