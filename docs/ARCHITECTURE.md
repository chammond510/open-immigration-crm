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

There is no Node build, SPA, Redis, Celery, message broker, AI provider, payment processor, email provider, cloud-drive API, or background worker today. The connector work described below adds a database-backed worker and optional provider adapters. It does not add Redis, Celery, or a message broker.

## Domain model

- `FirmProfile` is a singleton because one installation represents one firm.
- `Contact` represents a person in the inquiry/client lifecycle.
- `Matter` is case work with one primary contact and optional `MatterParty` records.
- `Activity` is an append-oriented note/status/event stream.
- `WorkItem` unifies a task, deadline, or appointment.
- `TimeEntry` records tracked working time against a matter or contact; one timer runs per user, and no rates or amounts are stored.
- `ChecklistItem` is firm-authored and scoped to a single matter.
- `Document` stores metadata in the database and bytes in private file storage.
- `IntakeForm` stores firm-created questions; `IntakeInvite` stores only a SHA-256 token hash; `IntakeSubmission` stores the received answers.
- `AuditLog` records important user actions. It is not a complete forensic event stream.
- `TwoFactorDevice` holds a user's authenticator secret, encrypted with `FIELD_ENCRYPTION_KEYS`; `RecoveryCode` stores only SHA-256 hashes of single-use recovery codes.

## Trust boundaries

All ordinary CRM pages require an active staff account. Destructive document/party actions, firm settings, and exports require a superuser. A public intake URL is a bearer credential: possession allows one submission until the link expires or is revoked. Intake answers are displayed as unverified client-provided information.

Uploaded files are never routed through Django's public media URL. Downloads pass through an authenticated view and return `private, no-store`.

## Connectors (planned)

The [connector plan](CONNECTOR_PLAN.md) adds optional integrations without changing the shape of core:

- `crm/connect/`: a provider-neutral framework. It holds the connector registry, connections with encrypted credentials, the outbound policy, the webhook endpoint, OAuth, and the Connections page.
- `connectors/<provider>/`: one optional adapter package per provider, implementing capability interfaces such as phone, mailbox, or payments. Third-party connectors install as Python packages through an entry point.
- A `worker` process with a database-backed task queue for syncs and webhook processing. Installations with nothing connected do not need it.
- A `/hooks/` path for provider webhooks and an MCP endpoint. Operators can expose just these paths through the reverse proxy without exposing the rest of the application.

Core never imports from `connectors/`; `scripts/check_scope.sh` enforces this.

## Extension policy

Provider code belongs in `connectors/`, never in core. New core dependencies require a clear security and maintenance benefit. A provider SDK may be added only as an optional extra for its connector. Schema changes use ordinary checked-in Django migrations. Operators should run migrations as a separate release step before replacing application processes.
