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

There is no Node build, SPA, Redis, Celery, message broker, AI provider, payment processor, email provider, cloud-drive API, or background worker.

## Domain model

- `FirmProfile` is a singleton because one installation represents one firm.
- `Contact` represents a person in the inquiry/client lifecycle.
- `Matter` is case work with one primary contact and optional `MatterParty` records.
- `Activity` is an append-oriented note/status/event stream.
- `WorkItem` unifies a task, deadline, or appointment.
- `ChecklistItem` is firm-authored and scoped to a single matter.
- `Document` stores metadata in the database and bytes in private file storage.
- `IntakeForm` stores firm-created questions; `IntakeInvite` stores only a SHA-256 token hash; `IntakeSubmission` stores the received answers.
- `AuditLog` records important user actions. It is not a complete forensic event stream.

## Trust boundaries

All ordinary CRM pages require an active staff account. Destructive document/party actions, firm settings, and exports require a superuser. A public intake URL is a bearer credential: possession allows one submission until the link expires or is revoked. Intake answers are displayed as unverified client-provided information.

Uploaded files are never routed through Django's public media URL. Downloads pass through an authenticated view and return `private, no-store`.

## Extension policy

Prefer a separate project or narrow adapter over adding providers to core. New core dependencies require a clear security and maintenance benefit. Schema changes use ordinary checked-in Django migrations. Operators should run migrations as a separate release step before replacing application processes.
