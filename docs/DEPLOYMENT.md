# Deployment

The supplied Compose configuration is for local evaluation and binds the application only to `127.0.0.1`. A real deployment requires infrastructure decisions this repository cannot safely make for every firm.

## Production baseline

- A supported container host or Python 3.12+ environment
- PostgreSQL 16 or newer with encrypted storage and connections
- A persistent, encrypted media volume
- An HTTPS reverse proxy that forwards `X-Forwarded-Proto: https`
- Restricted network access and an MFA-capable identity or VPN layer
- Encrypted offsite backups and monitoring

## Environment

Set at least:

```text
DEBUG=False
SECRET_KEY=<unique random value of at least 50 characters>
FIELD_ENCRYPTION_KEYS=<generated encryption key>
ALLOWED_HOSTS=crm.example.org
CSRF_TRUSTED_ORIGINS=https://crm.example.org
DATABASE_URL=postgresql://user:password@database-host:5432/database?sslmode=require
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
TRUST_X_FORWARDED_PROTO=True
TIME_ZONE=America/Chicago
```

`TRUST_X_FORWARDED_PROTO=True` tells the application to believe the reverse
proxy's `X-Forwarded-Proto: https` header. Set it only behind a proxy that
always overwrites that header; leaving it unset on a proxied deployment makes
`SECURE_SSL_REDIRECT` loop, and setting it on a directly exposed server lets
clients spoof HTTPS.

The application sends a one-year HSTS policy in production. Set
`SECURE_HSTS_INCLUDE_SUBDOMAINS=True` and `SECURE_HSTS_PRELOAD=True` only after
confirming every subdomain is permanently HTTPS and you understand the browser
preload program; both options intentionally default to `False`.

## Encryption key

`FIELD_ENCRYPTION_KEYS` encrypts stored secrets, such as each user's two-factor key. The application refuses to start in production without it. Generate one with:

```bash
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Store it in your secret manager and back it up separately from database backups. A database backup without its key cannot reveal those secrets, and a lost key cannot be recovered: every user's two-factor sign-in would have to be reset.

To rotate, put a new key first and keep the old one after a comma (`FIELD_ENCRYPTION_KEYS=new,old`), restart, run `python manage.py rotate_field_encryption`, then remove the old key and restart again.

Do not place production secrets in `.env` on a shared workstation or in Git. Use the secret facility of your deployment system. Set file and directory permissions so only the application identity can read the media volume.

## Release sequence

Build an immutable image from a reviewed tag. Before changing live processes:

```bash
python manage.py migrate --noinput
python manage.py check --deploy
```

Then start `gunicorn config.wsgi:application` (the included `scripts/start.sh` does this). Run migrations once as a release job—not independently in every web process. Check `/health/` through the private load-balancer path; it intentionally reveals only `ok` and does not test every dependency.

Create the first administrator before the service is reachable by anyone but you. Two paths:

- **Setup screen:** while no accounts exist, the application serves a one-time administrator-creation page and the login page redirects to it. It disables permanently once the first account exists. Complete it immediately after the first start; do not leave an account-less installation reachable on a shared network.
- **Console job**, for scripted or headless installs:

```bash
python manage.py bootstrap_admin \
  --username admin \
  --email administrator@example.org \
  --password '<unique value from your password manager>'
```

Do not put the password in reusable shell history. Change it immediately if the platform logs command arguments.

## Staff and roles

Every active staff account sees and works with every contact, matter, and work item. A role adds what goes beyond that everyday work. Roles are Django groups, and the CRM creates four presets:

| Permission | What it allows | Managing partner | Attorney | Paralegal | Office administrator |
|---|---|:-:|:-:|:-:|:-:|
| Billing | Invoices, payments, and payment plans | ✓ | ✓ | | ✓ |
| Voids and rates | Void invoices and payments, and set billing rates | ✓ | | | ✓ |
| Delete records | Delete documents, communications, notices, time entries, and matter parties, and remove a workflow from a matter | ✓ | ✓ | | ✓ |
| Import | Import contacts and matters | ✓ | | | ✓ |
| Export | Download every contact and matter as a spreadsheet | ✓ | | | |
| Firm settings | Firm settings, workflows, and custom fields | ✓ | | | ✓ |
| Connections and AI | Firm connections, AI features, AI assistants, and the API | ✓ | | | |
| Manage staff | Add people, change their roles, and deactivate accounts | ✓ | | | ✓ |

Superusers hold every permission, and only they see **Administration**. The first administrator is a superuser. To change a preset or add a role of your own, a superuser edits it under **Administration → Groups**; the permissions are listed under "crm | firm profile" and "billing | invoice". Staff who open a page their role does not include see a page that says so.

### The Team page

People whose role includes **Manage staff** run the team from **Settings → Team**:

- **Add a person** with their name, email, and role. Their username is their email address, and the account has no password until they choose one. If the firm has connected an email delivery service (a connection with the System email capability), the CRM emails them a link to choose a password. Otherwise, or if the email is held in preview mode or refused, the link is shown once to whoever added them, to send through a channel the firm trusts. The link works once and expires after three days (Django's `PASSWORD_RESET_TIMEOUT`); sending a new invitation ends the earlier link.
- **Change a role**, name, or email. Each person has one role, or none.
- **Reset two-factor sign-in** for someone who lost their phone and recovery codes.
- **Deactivate** someone who leaves. Their open work items, open matters, and active contacts go to a colleague you choose, or stay unassigned. They are signed out everywhere, their API tokens and AI assistant sign-ins stop working, a running timer of theirs stops, and their personal connections, such as their own mailbox and calendar, are disconnected; what those brought in stays. **Reactivate** brings the account back without giving work back.

Nobody can give a role with a permission they do not hold, or change someone who holds a permission they do not, so an office administrator can add attorneys and paralegals but not a managing partner. Nobody changes their own role or deactivates themselves, so whoever manages staff cannot lock the firm out. Only a superuser changes a superuser. Every change is in the audit log.

Each person edits their own name and email under **Profile** in the menu under their name. When their username is their email address, changing the email changes the username too.

Upgrading creates the presets and changes nobody's access: existing staff have no role, and so only everyday work, until someone gives them one, and billing access granted to a person directly is kept.

## Sign-in lockout

Repeated failed sign-ins lock the username and client address pair for a cooling-off period (default: five failures, fifteen minutes; tune with `AXES_FAILURE_LIMIT` and `AXES_COOLOFF_MINUTES`). Clear a lockout early with:

```bash
python manage.py axes_reset
```

Lockout tracking uses the same proxy-aware client address as the audit log, so the reverse proxy must append the real client address to `X-Forwarded-For`. Lockout slows online guessing; it does not replace two-factor sign-in or network-layer access controls.

## Two-factor sign-in

Each user can turn on two-factor sign-in under **Two-factor sign-in** in the sidebar, using any authenticator app. Someone who manages firm settings can require it for all staff in **Firm settings**; staff without it are then sent to set it up before they can use the CRM. Five incorrect codes in a row lock the second step for the same cooling-off period as a password lockout (tune with `TWO_FACTOR_FAILURE_LIMIT`).

If someone loses their phone and their recovery codes, someone who manages staff resets their two-factor sign-in on their page under **Settings → Team**. It can also be reset from the console, or by deleting their entry under **Two-factor devices** in administration:

```bash
python manage.py reset_two_factor <username>
```

They can then sign in with their password and set it up again. Each path is recorded in the audit log.

## Connector worker

Connectors sync and process provider events, and AI features run, in a separate worker process:

```bash
python manage.py run_worker
```

The Compose file runs it as the `worker` service, using the same image and environment as `web`. The worker keeps its queue in the application database, so it needs no Redis or message broker. Installations with nothing connected can stop it (`docker compose stop worker`). Run one worker per installation unless you have measured a need for more; each one records a heartbeat (default every `WORKER_TICK_SECONDS=60`) so the CRM can tell when syncs have stopped.

## Billing access

Billing is visible to staff whose role includes **Billing** (the "Can view and manage billing" permission), which the Managing partner, Attorney, and Office administrator presets do. Staff whose role includes **Voids and rates** void invoices and payments and set the firm default rate, staff rates, payment terms, and payment instructions under **Billing → Billing settings**.

## Connecting services

Staff whose role includes **Connections and AI** connect firm-wide services on the **Connections** page; all staff can connect services that are personal to them, such as their own calendar. For providers that use OAuth sign-in, someone who manages connections and AI first registers the firm's own app in the provider's developer console. The connector's page shows the exact redirect URI to enter there (`https://<your-host>/connections/oauth/callback/`); that URI must be reachable by staff browsers, not by the provider. The fictional example connectors appear in the catalog only when `CONNECTORS_SHOW_HIDDEN=true`, which defaults to on only with `DEBUG=true`. Beta connectors that wait on a provider's confirmation are offered only when their flag is listed in `CONNECTOR_FLAGS`, separated by commas: `CONNECTOR_FLAGS=chatgpt_plan` offers the ChatGPT plan connector (see [AI features](AI.md)). Removing a flag stops that connector's connections.

## Provider webhooks

Providers deliver events to `https://<your-host>/hooks/<token>/`, where each connection has its own token. If the rest of the CRM is reachable only through a VPN or identity-aware proxy, expose just the `/hooks/` path publicly at the reverse proxy and keep everything else private. Requests without a valid provider signature are rejected and nothing is stored. Bodies over `CONNECTOR_WEBHOOK_MAX_BYTES` (default 1 MB) are refused; keep the proxy's request-body limit for `/hooks/` at or below that value.

Keep the trailing slash when entering a webhook URL at a provider; a redirect would drop the request body.

## API

The JSON API at `/api/v1/` is off until someone who manages connections and AI turns it on under **API access**. While it is off, every request is refused. See [API](API.md).
- **Tokens.** Each token acts as one staff member. Only people who manage connections and AI can create tokens unless one of them turns on **Let staff create their own tokens**.
- **Exposure.** Tools that run in the cloud, such as Zapier, must reach `/api/` over HTTPS. Expose that path at the reverse proxy only if the firm uses such a tool. Self-hosted tools like n8n can call the CRM on the private network instead.
- **Limits.** Each token is limited to `API_HOURLY_LIMIT` requests an hour (default 1000). Bodies over 256 KB are refused.
- **Request log.** Every request is logged. Old rows can be removed with `python manage.py prune_api_log --days 365`.

## Phone numbers

Phone numbers are matched in E.164 form. Numbers written without a country code are read as `PHONE_DEFAULT_REGION`'s (an ISO 3166 code; default `US`). Set it to the firm's own country.

## AI assistants (MCP)

The MCP server lets staff use the CRM from Claude or ChatGPT; see [AI assistants](MCP.md). It is off until someone who manages connections and AI turns it on under **AI assistants**.
- **Reachability.** Claude and ChatGPT call it from their own servers. Make these paths reachable over HTTPS:
  - `/mcp`
  - `/.well-known/oauth-authorization-server`
  - `/.well-known/oauth-protected-resource/mcp`
  - `/o/`

  The rest of the CRM can stay private: staff browsers need the consent page, `/o/authorize/`, but the assistants do not.
- **Correct addresses.** The proxy must pass the original host and `X-Forwarded-Proto` (set `TRUST_X_FORWARDED_PROTO=true`). Otherwise the addresses in the discovery documents will be wrong and assistants cannot sign in.
- **Limits.** Each person is limited to `MCP_HOURLY_LIMIT` tool calls an hour (default 1000).
- **Expired tokens.** Run `python manage.py cleartokens` from cron to delete expired tokens.

## Outbound webhooks

The **Webhooks** connector sends the CRM events that someone who manages connections and AI chooses to an automation tool's address; see [Outbound webhooks](WEBHOOKS.md).
- **Worker.** Deliveries need the worker running.
- **Network.** The worker makes outbound HTTPS requests to that address. Allow them through any egress firewall.
- **Self-hosted tools.** A tool on the firm's own network, such as n8n in the same Compose project, can be reached over plain `http://` by its service name.
- **Stopping deliveries.** `CONNECTORS_OUTBOUND_ENABLED=false` stops every delivery at once.

## Reverse proxy

Terminate TLS at the proxy and forward only to the private Gunicorn port. Preserve the original host, set `X-Forwarded-Proto` to `https`, cap request bodies consistently with `DOCUMENT_UPLOAD_MAX_BYTES`, disable caching for authenticated responses, and do not add third-party analytics to intake pages.

## Upgrade and rollback

Read release notes and back up first. Releases that add two-factor sign-in require `FIELD_ENCRYPTION_KEYS`; set it before upgrading, or the application will not start. Database migrations may be one-way. Prefer roll-forward with a corrective release; if rollback is required, restore both database and media from the same pre-upgrade recovery point. Never restore one without the other.
