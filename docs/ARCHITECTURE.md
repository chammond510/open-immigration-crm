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

There is no Node build, SPA, Redis, Celery, message broker, AI provider, payment processor, email provider, or cloud-drive API. An optional `worker` process (`python manage.py run_worker`) runs connector syncs and webhook processing from a queue kept in the application database; installations with nothing connected do not need it.

## Domain model

- `FirmProfile` is a singleton because one installation represents one firm.
- `Contact` represents a person in the inquiry/client lifecycle.
- `Matter` is case work with one primary contact and optional `MatterParty` records.
- `Activity` is an append-oriented note/status/event stream.
- `WorkItem` unifies a task, deadline, or appointment.
- `TimeEntry` records tracked working time against a matter or contact; one timer runs per user, and no rates or amounts are stored.
- `ChecklistItem` is firm-authored and scoped to a single matter.
- `Communication` (in `crm/comms/`) is a call, text, email, or meeting linked to a contact and optionally a matter, logged by staff or recorded by a connector (stored once per connection and provider ID). `Contact.texting_opted_out_at` records a texting opt-out, which the outbound policy enforces.
- `Document` stores metadata in the database and bytes in private file storage.
- `IntakeForm` stores firm-created questions; `IntakeInvite` stores only a SHA-256 token hash; `IntakeSubmission` stores the received answers.
- `AuditLog` records important user actions. It is not a complete forensic event stream.
- `Connection`, `OutboundRequest`, `SyncRun`, `WebhookEvent`, `WorkerHeartbeat`, and `ConnectorApp` (in `crm/connect/`) hold connected provider accounts, the outbound log, sync history, received provider events, and worker check-ins; see Connectors below.
- `TwoFactorDevice` holds a user's authenticator secret, encrypted with `FIELD_ENCRYPTION_KEYS`; `RecoveryCode` stores only SHA-256 hashes of single-use recovery codes.

## Trust boundaries

All ordinary CRM pages require an active staff account. Destructive document/party actions, firm settings, and exports require a superuser. A public intake URL is a bearer credential: possession allows one submission until the link expires or is revoked. Intake answers are displayed as unverified client-provided information.

Uploaded files are never routed through Django's public media URL. Downloads pass through an authenticated view and return `private, no-store`.

## Connectors

The [connector plan](CONNECTOR_PLAN.md) adds optional integrations without changing the shape of core. No provider connector has shipped yet. The framework is in place:

- `crm/connect/` is a provider-neutral Django app (label `connect`).
  - `registry.py` imports every module under the packages in `CONNECTOR_PACKAGES` (default `connectors`), plus installed packages that use the `open_immigration_crm.connectors` entry point. It rejects connectors whose manifest is incomplete, and it merges installed connectors with the wanted providers listed in `connectors/catalog.toml`.
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
- `connectors/<provider>/` holds one optional adapter package per provider. `connectors/example/` is a fictional connector, hidden from the catalog, that exercises the framework.

- **Worker.** `python manage.py run_worker` runs two things:
  - a scheduler thread that records a `WorkerHeartbeat` and queues a `SyncRun` for each due capability of each active connection;
  - the `django-tasks-db` worker, which runs syncs and webhook processing from a queue table in the application database.

  Each sync resumes from the previous successful run's cursor. Synced and webhook records are delivered through the `records_received` signal; features subscribe to it, and the framework keeps only the run or event record.
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

Still planned:
- an MCP endpoint

Operators can expose just `/hooks/` (and later the MCP path) through the reverse proxy without exposing the rest of the application.

Core never imports from `connectors/`; `scripts/check_scope.sh` enforces this.

## Extension policy

Provider code belongs in `connectors/`, never in core. New core dependencies require a clear security and maintenance benefit. A provider SDK may be added only as an optional extra for its connector. Schema changes use ordinary checked-in Django migrations. Operators should run migrations as a separate release step before replacing application processes.
