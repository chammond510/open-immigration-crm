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

## Sign-in lockout

Repeated failed sign-ins lock the username and client address pair for a cooling-off period (default: five failures, fifteen minutes; tune with `AXES_FAILURE_LIMIT` and `AXES_COOLOFF_MINUTES`). Clear a lockout early with:

```bash
python manage.py axes_reset
```

Lockout tracking uses the same proxy-aware client address as the audit log, so the reverse proxy must append the real client address to `X-Forwarded-For`. Lockout slows online guessing; it does not replace MFA or network-layer access controls.

## Reverse proxy

Terminate TLS at the proxy and forward only to the private Gunicorn port. Preserve the original host, set `X-Forwarded-Proto` to `https`, cap request bodies consistently with `DOCUMENT_UPLOAD_MAX_BYTES`, disable caching for authenticated responses, and do not add third-party analytics to intake pages.

## Upgrade and rollback

Read release notes and back up first. Database migrations may be one-way. Prefer roll-forward with a corrective release; if rollback is required, restore both database and media from the same pre-upgrade recovery point. Never restore one without the other.
