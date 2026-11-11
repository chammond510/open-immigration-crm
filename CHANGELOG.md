# Changelog

Releases are tagged on GitHub. Security fixes go to the latest tagged release (see [SECURITY.md](SECURITY.md)). Back up the database and the media volume before any upgrade, and read [Upgrade and rollback](docs/DEPLOYMENT.md#upgrade-and-rollback).

## 0.2.0 (2026-11-11)

The first release since 0.1.0. It turns the core CRM into a practice workspace with optional connectors, and it carries every security fix made since August.

### Before you upgrade from 0.1.0

- Set `FIELD_ENCRYPTION_KEYS` first. The application will not start in production without it. [Encryption key](docs/DEPLOYMENT.md#encryption-key) shows how to make one.
- Run `python manage.py migrate --noinput` once, as a release job. Some migrations are one-way, so a rollback means restoring the database and media from the same backup.
- Existing staff keep everyday access and have no role until someone gives them one. Billing access granted to a person directly is kept. See [Staff and roles](docs/DEPLOYMENT.md#staff-and-roles).
- Connectors, AI features, the API, and the MCP server are all off after the upgrade, as on a new installation.
- Connectors and AI features need the worker process (`python manage.py run_worker`). The Compose file runs it as the `worker` service.

### Security

- Two-factor sign-in with an authenticator app and recovery codes, which a firm can require for all staff.
- Encryption for stored secrets, with key rotation.
- Staff roles (Managing partner, Attorney, Paralegal, Office administrator) in place of superuser checks, and a Team page for invitations, role changes, and deactivation.
- Password reset by email, once the firm connects an email delivery service.
- Hardened data flows, authentication, and confidential HTTP handling: connector data-flow approval, request logs without paths or headers, and `no-store` on dynamic responses.
- The referrer policy is `strict-origin`. Under `no-referrer`, which main used from November 7 to November 10, browsers sent `Origin: null` and every form post was rejected.
- sqlparse 0.6.0, for known vulnerabilities in 0.5.5.

### Core

- A communications log of calls, texts, emails, and meetings, with a per-contact texting opt-out.
- Billing: rates, invoices, manually recorded payments, payment plans with reminders, and a printable invoice.
- USCIS notices on matters, response due dates in the work queue, and a receipt-number status history.
- Firm-written workflow templates and custom fields.
- One-click time tracking, shipments on matters, and saved research authorities.
- An operating dashboard built only from CRM data.
- Notifications when someone gives you work, and an overview that opens on your own work.
- Import of contacts and matters from a spreadsheet.
- A one-time setup screen for the first administrator.
- A one-command trial with fictional records: `sh scripts/try.sh`.

### Automation and AI, all off until the firm turns them on

- A JSON API with scoped, expiring tokens and a request log.
- Signed outbound webhooks, with recipes for n8n, Zapier, and Make.
- An MCP server so staff can use the CRM from Claude or ChatGPT on their own plans.
- AI features on the firm's own model connection: consult prep memos, task suggestions, and a matter assistant whose changes wait for confirmation.

### Connectors (Beta)

The connector framework, a database-backed worker, and the Connections page, with connectors for:

- **Phone and messaging:** Dialpad, Twilio, WhatsApp Business.
- **Email and calendar:** Google Workspace, Microsoft 365, IMAP and SMTP, an SMTP server for the CRM's own email.
- **Scheduling and meetings:** Calendly, Cal.com, Zoom.
- **Payments:** LawPay, Stripe.
- **Documents:** Documenso, Google Drive, OneDrive and SharePoint.
- **Case status and shipping:** the USCIS Case Status API, USPS, UPS, FedEx.
- **Research:** Passim, CourtListener.
- **Other:** AI models (Anthropic, OpenAI, OpenRouter, or any OpenAI-compatible endpoint), DeepL, Clio Manage import, a website form, OpenID Connect sign-in.

Every connector is Beta. Each was built and tested against recorded fixtures, and CI never calls a live service, so try a connector with your own account before relying on it. A [connector SDK](docs/CONNECTOR_SDK.md) lets anyone ship one as a package of their own.

## 0.1.0 (2026-08-11)

First public release: contacts and a pipeline, matters and parties, tasks, deadlines and appointments, firm-created checklists, protected document storage, secure one-use intake links, audit records, and CSV and JSON exports.
