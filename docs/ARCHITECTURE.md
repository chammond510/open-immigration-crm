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
- `Connection` and `OutboundRequest` (in `crm/connect/`) hold connected provider accounts and the log of outbound actions; see Connectors below.
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
    8. the hourly limit
    9. preview mode

    It records every attempt as an `OutboundRequest`, storing only safe error text.
  - `testing.ConnectorContract` holds the shared contract tests. They block network access.
- `connectors/<provider>/` holds one optional adapter package per provider. `connectors/example/` is a fictional connector, hidden from the catalog, that exercises the framework.

Still planned:
- a `worker` process with a database-backed task queue for syncs and webhook processing (installations with nothing connected will not need it)
- a `/hooks/` path for provider webhooks
- OAuth sign-in
- the Connections page
- an MCP endpoint

Operators will be able to expose just the webhook and MCP paths through the reverse proxy without exposing the rest of the application.

Core never imports from `connectors/`; `scripts/check_scope.sh` enforces this.

## Extension policy

Provider code belongs in `connectors/`, never in core. New core dependencies require a clear security and maintenance benefit. A provider SDK may be added only as an optional extra for its connector. Schema changes use ordinary checked-in Django migrations. Operators should run migrations as a separate release step before replacing application processes.
