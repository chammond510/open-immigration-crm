# Security model

Read this before storing real client information.

## Protected assets

The system can hold names, contact information, birth dates, addresses, nationality and immigration information, A-numbers, receipt numbers, attorney notes, deadlines, time records, intake answers, and uploaded documents. Treat the entire database, media volume, logs, exports, and backups as confidential.

## Controls provided by the application

- Staff authentication through Django's mature session framework
- A first-run setup screen that permanently disables once the first account exists
- Login rate limiting with a temporary lockout after repeated failures
- Two-factor sign-in with an authenticator app (TOTP), single-use recovery codes stored only as hashes, replay protection, and a lockout after repeated incorrect codes
- A firm setting that requires two-factor sign-in for all staff; sessions that skip the second step are signed out
- Encryption of stored secrets, such as two-factor keys, with keys kept outside the database and support for key rotation
- Superuser restriction for exports, firm settings, and destructive record/file actions
- CSRF protection on state-changing browser requests
- Secure/HTTP-only/SameSite cookies and HTTPS redirect/HSTS when production settings are enabled
- A restrictive content security policy and related browser headers
- Random intake tokens stored only as SHA-256 hashes, with expiry, revocation, and one-use enforcement
- Transactional locking when accepting an intake submission
- File-size, extension, and magic-byte checks for PDF, DOCX, PNG, and JPEG
- Authenticated document delivery with non-cache headers
- Audit events for important data actions and downloads
- Production configuration checks and dependency/security automation

## Important limitations

- **No tenant isolation.** One deployment is one firm. All staff can access all ordinary CRM records.
- **No SSO, and two-factor sign-in is off until enabled.** Require two-factor sign-in in Firm settings, or put the application behind an identity-aware proxy or VPN with MFA.
- **Client records are not encrypted at the field level.** Only stored secrets are. Use encrypted disks/volumes, encrypted database service storage, TLS to the database, and encrypted backups.
- **The encryption key is only as safe as its storage.** Anyone with both the database and `FIELD_ENCRYPTION_KEYS` can read stored secrets. Keep the key in a secret manager, apart from database backups.
- **No malware scanner.** File signature validation is not antivirus. Add a scanning/quarantine layer before enabling uploads in a higher-risk environment.
- **No immutable audit store.** A server/database administrator can alter audit rows. Export logs to protected external monitoring if you require tamper resistance.
- **No automated retention or legal hold.** The firm must define and execute its own retention, preservation, and deletion policy.
- **No communication delivery.** Staff manually copy intake links into a separately approved communication channel.
- **No legal correctness controls.** Checklists and intake questions are firm-authored; the software does not validate legal content.

## Required deployment controls

1. HTTPS only, with a current TLS configuration and correct proxy headers.
2. Unique 50+ character Django secret; never reuse a checked-in/example value.
3. PostgreSQL with least-privilege credentials and encrypted connections/storage.
4. Restricted network exposure. Prefer VPN/zero-trust access over an unrestricted public login page.
5. Complete first-run setup (or create the administrator from the console) before the service is reachable by anyone else.
6. Two-factor sign-in required for all staff (Firm settings) or MFA at the access layer, and individual named staff accounts.
7. Prompt OS, container, Python dependency, PostgreSQL, and application patching.
8. Encrypted, tested, offsite backups of both database and media files.
9. Monitoring for authentication anomalies, server errors, disk capacity, backup failure, and certificate expiry.
10. A documented offboarding process that disables staff accounts immediately.
11. A tested incident-response plan appropriate for attorney/client information.

## Connectors (planned)

No connector has shipped. Two-factor sign-in and encryption of stored secrets, which connectors depend on, are in place. Connectors change the risk picture in these ways:

- **Data leaves the installation.** Each connector sends data to a provider the firm chooses. The connect screen lists what is sent and received, and a superuser must acknowledge it. The firm remains responsible for evaluating each provider and its data-use terms. Consumer and business AI plans often have different terms.
- **Some features need public reachability.** Provider webhooks and the MCP endpoint must be reachable from the provider's network. Expose only those paths through the reverse proxy, use polling where a provider supports it, or leave those connectors off.
- **More credentials to protect.** Connection credentials will be stored encrypted, scoped to the capabilities the firm turns on, and deleted on disconnect. They use the same `FIELD_ENCRYPTION_KEYS` encryption as two-factor keys.
- **Outbound actions are gated.** Sending messages, taking payments, writing calendar events, and calling AI models will pass an outbound policy and start in preview mode.
- **Webhooks fail closed.** A webhook with a missing or invalid signature is rejected in every environment, including development settings. Each connection has its own unguessable URL token, stored only as a hash. A verified event's payload is stored minimized and encrypted, and deleted once the worker has processed it.

## Intake link handling

An intake URL is sensitive. Send it only to its intended recipient, keep expiry short, revoke it if misdirected, and do not place it in analytics or third-party link shorteners. The default referrer policy and CSP reduce browser leakage, but the recipient's device and delivery channel remain outside this application's control.
