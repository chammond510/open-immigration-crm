# Open Immigration CRM

A focused, self-hosted practice workspace for small immigration law firms. It keeps the essential loop—people, matters, work, documents, checklists, and secure intake—without AI, billing, communications providers, or a maze of integrations.

> **Early release:** evaluate carefully before storing real client information. The operators of each installation are responsible for security, backups, retention, professional-responsibility compliance, and the legal content they create.

## What it includes

- Staff-only contacts and a simple contact pipeline
- Matters, primary contacts, additional parties, status history, and notes
- Tasks, deadlines, and appointments in one work queue
- One-click time tracking against a matter or contact, with per-record totals
- Firm-created per-matter checklists—zero legal templates ship with the project
- Private PDF, DOCX, PNG, and JPEG document storage
- Blank intake-form builder with expiring, revocable, single-use links
- Audit records for important actions
- CSV and portable JSON export paths
- PostgreSQL production path and SQLite-only local evaluation path

## Quick start with Docker

Requirements: Docker with Compose.

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Put the generated value in SECRET_KEY and change POSTGRES_PASSWORD in .env.
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

This application stores highly sensitive information. It provides authenticated staff access, login rate limiting with temporary lockout, secure-cookie production defaults, CSRF protection, browser security headers, hashed intake tokens, restricted file types, non-public document delivery, and audit events. It does **not** provide field-level database encryption, tenant isolation, built-in MFA, SSO, malware scanning, or a managed backup service. Use full-disk/database encryption, MFA at the identity or network layer, a trusted private network when possible, and encrypted offsite backups.

Read [Security model](docs/SECURITY_MODEL.md), [Privacy](docs/PRIVACY.md), and [Backup and restore](docs/BACKUP_RESTORE.md) before using real information. Report vulnerabilities privately under [SECURITY.md](SECURITY.md).

## License

Copyright © 2026 Open Immigration CRM contributors.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). If you modify and operate it for users over a network, the AGPL generally requires offering those users the corresponding source of your running version. This summary is not legal advice; the license text controls.

The software comes with **no warranty**. It is not legal advice and does not create an attorney-client relationship.
