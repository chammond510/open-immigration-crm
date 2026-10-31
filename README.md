# Open Immigration CRM

A focused, self-hosted practice workspace for small immigration law firms. Its core keeps the essential loop—people, matters, work, documents, checklists, and secure intake—small enough for a firm to understand, operate, and audit. Integrations with the services a firm already uses are being added as optional connectors that stay off until the firm connects them.

> **Early release:** evaluate carefully before storing real client information. The operators of each installation are responsible for security, backups, retention, professional-responsibility compliance, and the legal content they create.

## What it includes

- Staff-only contacts and a simple contact pipeline
- Matters, primary contacts, additional parties, status history, and notes
- Tasks, deadlines, and appointments in one work queue
- USCIS notices on each matter (receipts, appointments, RFEs, NOIDs, decisions), with response due dates and appointments added to the work queue, and a receipt-number status history
- A communications log of calls, texts, emails, and meetings, with a per-contact texting opt-out. Records from connected services are matched to contacts by exact phone number or email, and anything unmatched waits in a Needs matching list
- One-click time tracking against a matter or contact, with per-record totals
- Billing for staff granted access: hourly rates, invoices whose lines are marked earned fee or trust advance, manually recorded payments with the account they were deposited to, payment plans with staff reminders (and client email reminders once the firm connects an email service), and a printable invoice
- Firm-created per-matter checklists—zero legal templates ship with the project
- Firm-written workflow templates: phases with checklist items and optional phase deadlines, applied to a matter by hand or automatically for new matters of a case type
- Custom fields on contacts and matters (text, number, date, choice, yes or no), shown on forms and record pages and carried in exports, the API, and webhooks
- Private PDF, DOCX, PNG, and JPEG document storage
- Blank intake-form builder with expiring, revocable, single-use links
- Optional two-factor sign-in with an authenticator app and recovery codes, which the firm can require for all staff
- A JSON API for scripts and automation tools, off until a superuser turns it on, with scoped, expiring, revocable tokens and a log of every request (see [API](docs/API.md))
- Signed outbound webhooks for automation tools: the firm chooses which CRM events are sent, and each one is retried and recorded (see [Outbound webhooks](docs/WEBHOOKS.md))
- An MCP server so staff can work with the CRM from Claude or ChatGPT on their own plans. It is off until a superuser turns it on. Each person signs in as themselves, read tools come first, and write tools need a superuser's approval and the person's confirmation (see [AI assistants](docs/MCP.md)).
- An operating dashboard built only from CRM data:
  - intake by stage, with lead sources from a custom field;
  - matters by status, case type, and workflow phase;
  - deadlines and open RFE or NOID responses;
  - USCIS notices, communications, and time;
  - for staff with billing access, invoiced, collected, and outstanding amounts by age.

  Each figure is compared with the previous period.
- Audit records for important actions
- CSV and portable JSON export paths
- Import of contacts and matters from a spreadsheet, with a preview and a report of every row skipped and why ([guide](docs/SPREADSHEET_IMPORT.md))
- PostgreSQL production path and SQLite-only local evaluation path

## Core and connectors

Core is everything listed above. It works on its own, makes no outbound calls to outside services, and includes no AI.

The **Connections** page lists the services the CRM can connect to; see the [connector plan](docs/CONNECTOR_PLAN.md). Each connector links the CRM to a service the firm chooses, such as a phone system, mailbox, calendar, payment processor, e-signature service, or AI model, and automate work staff can already do by hand. Connectors are designed to be:

- **Optional.** Nothing is connected on a new installation, and every feature keeps a manual path.
- **Provider-neutral.** Core code talks to a capability such as "phone" or "payments", and each provider is an adapter in `connectors/`. Core never imports provider code; `scripts/check_scope.sh` enforces this.
- **Explicit about data.** Each connector states what it sends and receives, and a superuser acknowledges that before turning it on.
- **Cautious by default.** Anything a connector would send out starts in preview mode, and AI features are opt-in and off by default.

The connector framework, background worker, webhooks, and Connections page are in place. Connectors so far:

- **Webhooks**, built in and vendor-neutral, sends signed CRM events to n8n, Zapier, Make, or any address the firm chooses. [Automation recipes](docs/recipes/README.md) show how.
- **USCIS Case Status API** (Beta) checks the receipt numbers of open matters once a day through the firm's own developer.uscis.gov app, and creates a review task when a status changes.
- **Dialpad** (Beta) files calls, with recording links and Dialpad's AI recap, and texts on the right contact.
- **Twilio** (Beta) files incoming texts and STOP replies, and can log the firm's Twilio calls on the right contact.
- With Dialpad or Twilio connected, staff can send texts from contact and matter pages. Each text passes the outbound policy, and its delivery status is shown.
- **AI models** (Beta): Anthropic, OpenAI, OpenRouter, or any OpenAI-compatible endpoint, including a model the firm runs itself. Behind a flag, staff can also use their own ChatGPT plan through Sign in with ChatGPT, until OpenAI confirms this use. They power consult prep memos, task suggestions that staff accept or dismiss, and a matter assistant whose changes wait for confirmation and whose drafts are never sent. AI features stay off until a superuser turns them on, and every request is logged with the connection that paid for it (see [AI features](docs/AI.md)).
- **LawPay** (Beta) makes payment links for invoices, with advances paid into trust and earned fees into operating, and records LawPay payments and refunds on the invoice.
- **Stripe** (Beta) makes payment links for invoices and records payments and refunds. Each Stripe account pays into one account, operating by default, because Stripe takes its fees from each payment (see [the setup guide](connectors/stripe/README.md) before using one for trust).
- **Calendly** (Beta) files consultations booked through Calendly: the contact is matched or created, the consult appointment is made, reschedules and cancellations follow, and the booking link's tracking can fill the lead source.
- **Cal.com** (Beta) does the same for consultations booked through Cal.com, hosted or self-hosted (see [the setup guide](connectors/calcom/README.md)).
- **Website form** (Beta) turns signed submissions from the firm's website form into new leads, with the lead source and the answers kept on the contact (see [the setup guide](connectors/website_form/README.md)).
- **BIA Edge** (Beta) searches immigration case law, unpublished decisions, statutes, and policy manuals from a matter, through the firm's own BIA Edge API key. Staff save the authorities that matter to the matter, with citations, source links, and a note. Without a research service, staff can add authorities by hand.
- **CourtListener** (Beta) searches case law from the Free Law Project, including the federal courts of appeals, the Supreme Court, and the Board of Immigration Appeals. Each staff member connects their own CourtListener account, as its terms require.
- **Google Workspace** and **Microsoft 365** (Beta) connect each staff member's Gmail and Google Calendar, or Outlook mail and calendar. Their email with clients is filed on the right contact, they can send email from contact pages, and their appointments are kept in step with their calendar.
- **IMAP and SMTP email** (Beta) does the same for a mailbox at almost any other provider, with an app password (see [the setup guide](connectors/imap_smtp/README.md)).
- **SMTP server** (Beta) lets the CRM email clients itself, through the firm's mail server or an email delivery service such as Postmark, Amazon SES, SendGrid, or Mailgun: secure intake links when staff create them, and payment plan reminders when a plan asks for them (see [the setup guide](connectors/smtp/README.md)).
- **Zoom** (Beta) makes a Zoom meeting on each staff member's own account for their appointments, keeps its join link on the appointment, and moves or removes it with the appointment (see [the setup guide](connectors/zoom/README.md)). Without it, staff can paste any meeting link on an appointment.
- **OpenID Connect sign-in** (Beta) lets staff sign in with the firm's identity provider, such as Google Workspace or Microsoft Entra ID. Only existing staff accounts can sign in this way, linked the first time by an email address the provider vouches for; passwords and the CRM's own two-factor sign-in keep working (see [the setup guide](connectors/oidc/README.md)).
- **Documenso** (Beta) sends a matter's PDF to the matter's contacts for signature, through Documenso's hosted service or the firm's own, shows where each request stands, and files the signed PDF on the matter (see [the setup guide](connectors/documenso/README.md)). Without it, staff upload the signed copy themselves.
- **Google Drive** (Beta) copies each matter's documents, one way, into a folder for the matter on the firm's Drive, ideally a shared drive, renames the folder with the matter, and moves deleted documents' copies to the trash (see [the setup guide](connectors/google_drive/README.md)). The CRM stays the record; nothing is read back.
- **OneDrive and SharePoint** (Beta) does the same through Microsoft Graph, into a folder in a SharePoint document library or OneDrive the firm chooses (see [the setup guide](connectors/onedrive_sharepoint/README.md)).

The catalog lists the other providers people have asked for as Wanted. Anyone can build one as a package of their own; see [the connector SDK guide](docs/CONNECTOR_SDK.md).

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

This application stores highly sensitive information. It provides authenticated staff access, optional or firm-required two-factor sign-in, login rate limiting with temporary lockout, encryption of stored secrets, secure-cookie production defaults, CSRF protection, browser security headers, hashed intake tokens, restricted file types, non-public document delivery, and audit events. It does **not** encrypt client records at the field level, and it does not provide tenant isolation, malware scanning, or a managed backup service. Sign-in through the firm's identity provider is available as an optional connector. Use full-disk/database encryption, a trusted private network when possible, and encrypted offsite backups.

Read [Security model](docs/SECURITY_MODEL.md), [Privacy](docs/PRIVACY.md), and [Backup and restore](docs/BACKUP_RESTORE.md) before using real information. Report vulnerabilities privately under [SECURITY.md](SECURITY.md).

## License

Copyright © 2026 Open Immigration CRM contributors.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). If you modify and operate it for users over a network, the AGPL generally requires offering those users the corresponding source of your running version. This summary is not legal advice; the license text controls.

The software comes with **no warranty**. It is not legal advice and does not create an attorney-client relationship.
