# Open Immigration CRM

A focused, self-hosted practice workspace for small immigration law firms. Its core keeps the essential loop—people, matters, work, documents, checklists, and secure intake—small enough for a firm to understand, operate, and audit. Integrations with the services a firm already uses are being added as optional connectors that stay off until the firm connects them.

> **Early release:** evaluate carefully before storing real client information. The operators of each installation are responsible for security, backups, retention, professional-responsibility compliance, and the legal content they create.

## What it includes

- Staff-only contacts and a simple contact pipeline
- Matters, primary contacts, additional parties, status history, and notes
- Tasks, deadlines, and appointments in one work queue
- A communications log of calls, texts, emails, and meetings, with a per-contact texting opt-out
- One-click time tracking against a matter or contact, with per-record totals
- Billing for staff granted access: hourly rates, invoices whose lines are marked earned fee or trust advance, manually recorded payments with the account they were deposited to, payment plans with staff reminders, and a printable invoice
- Firm-created per-matter checklists—zero legal templates ship with the project
- Private PDF, DOCX, PNG, and JPEG document storage
- Blank intake-form builder with expiring, revocable, single-use links
- Optional two-factor sign-in with an authenticator app and recovery codes, which the firm can require for all staff
- Audit records for important actions
- CSV and portable JSON export paths
- PostgreSQL production path and SQLite-only local evaluation path

## Core and connectors

Core is everything listed above. It works on its own, makes no outbound calls to outside services, and includes no AI.

The **Connections** page lists the services the CRM can connect to; see the [connector plan](docs/CONNECTOR_PLAN.md). Each connector links the CRM to a service the firm chooses, such as a phone system, mailbox, calendar, payment processor, e-signature service, or AI model, and automate work staff can already do by hand. Connectors are designed to be:

- **Optional.** Nothing is connected on a new installation, and every feature keeps a manual path.
- **Provider-neutral.** Core code talks to a capability such as "phone" or "payments", and each provider is an adapter in `connectors/`. Core never imports provider code; `scripts/check_scope.sh` enforces this.
- **Explicit about data.** Each connector states what it sends and receives, and a superuser acknowledges that before turning it on.
- **Cautious by default.** Anything a connector would send out starts in preview mode, and AI features are opt-in and off by default.

The connector framework, background worker, webhooks, and Connections page are in place. No connector for a real provider has shipped yet; the catalog lists those people have asked for as Wanted.

## Quick start with Docker

Requirements: Docker with Compose.

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Put the generated value in SECRET_KEY and change POSTGRES_PASSWORD in .env.
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
# Put this value in FIELD_ENCRYPTION_KEYS and keep a copy apart from your backups.
docker compose build
docker compose run --rm web ./scripts/release.sh
docker compose up -d
```

Open [http://localhost:8000](http://localhost:8000) and create the administrator account on the one-time setup screen; it disables itself permanently once the first account exists. (Scripted installs can use `python manage.py bootstrap_admin` instead.) The Compose port binds only to the host loopback interface. Production requires an HTTPS reverse proxy; see [Deployment](docs/DEPLOYMENT.md).

To evaluate with fictional records after creating the administrator (replace `admin` with the username you created):

```bash
docker compose run --rm web python manage.py seed_demo --username admin
```

The seed command refuses to run when CRM records already exist. Its records are fictional and are not legal templates.

## Local development

Python 3.12+ is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.lock
export DEBUG=true  # development mode is opt-in; without it the app refuses to start unconfigured
python manage.py migrate
python manage.py bootstrap_admin --username admin --password 'use-a-unique-long-password'
python manage.py runserver
```

Run the complete local release gate with `./scripts/check_release.sh`.

## Security position

This application stores highly sensitive information. It provides authenticated staff access, optional or firm-required two-factor sign-in, login rate limiting with temporary lockout, encryption of stored secrets, secure-cookie production defaults, CSRF protection, browser security headers, hashed intake tokens, restricted file types, non-public document delivery, and audit events. It does **not** encrypt client records at the field level, and it does not provide tenant isolation, SSO, malware scanning, or a managed backup service. Use full-disk/database encryption, a trusted private network when possible, and encrypted offsite backups.

Read [Security model](docs/SECURITY_MODEL.md), [Privacy](docs/PRIVACY.md), and [Backup and restore](docs/BACKUP_RESTORE.md) before using real information. Report vulnerabilities privately under [SECURITY.md](SECURITY.md).

## License

Copyright © 2026 Open Immigration CRM contributors.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). If you modify and operate it for users over a network, the AGPL generally requires offering those users the corresponding source of your running version. This summary is not legal advice; the license text controls.

The software comes with **no warranty**. It is not legal advice and does not create an attorney-client relationship.
