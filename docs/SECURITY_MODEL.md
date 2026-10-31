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
- Billing visible only to superusers and staff granted the "Can view and manage billing" permission; voiding invoices or payments and changing rates are superuser-only, and every billing change is audited
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
- **Two-factor sign-in is off until enabled.** Require two-factor sign-in in Firm settings, or put the application behind an identity-aware proxy or VPN with MFA. Staff can also sign in through the firm's identity provider with the OpenID Connect connector; only existing staff accounts can, linked the first time by an email address the provider vouches for, and the CRM's own two-factor step still applies unless the connection is set to accept the provider's reported second factor. Keep a superuser with a password and the CRM's two-factor sign-in for when the provider cannot be reached.
- **Client records are not encrypted at the field level.** Only stored secrets are. Use encrypted disks/volumes, encrypted database service storage, TLS to the database, and encrypted backups.
- **The encryption key is only as safe as its storage.** Anyone with both the database and `FIELD_ENCRYPTION_KEYS` can read stored secrets. Keep the key in a secret manager, apart from database backups.
- **No malware scanner.** File signature validation is not antivirus. Add a scanning/quarantine layer before enabling uploads in a higher-risk environment.
- **No immutable audit store.** A server/database administrator can alter audit rows. Export logs to protected external monitoring if you require tamper resistance.
- **No automated retention or legal hold.** The firm must define and execute its own retention, preservation, and deletion policy.
- **No communication delivery unless the firm connects it.** Staff copy intake links into a separately approved communication channel. If the firm connects an email delivery service, staff can have the CRM email a new intake link to the contact's address on file instead; the link is still never stored or logged.
- **No trust ledger.** Billing records which account each payment was deposited to and which invoice lines are advances, but it does not reconcile a trust account. Keep trust accounting in a system built for it.
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

## Connectors

The connector framework and Connections page are in place, with Beta connectors for USCIS Case Status, Twilio, Dialpad, Calendly, Google Workspace, Microsoft 365, and LawPay. Controls in place:
- Only superusers can create firm-wide connections, acknowledge data flows, change outbound settings, or enter OAuth app credentials.
- Staff manage only their own personal connections.
- A personal connection, such as a staff member's own mailbox and calendar, sends only what its owner sends. The one thing it does on its own is keep the owner's records in step on the owner's account, such as putting their appointments on their calendar. It never runs the firm's automations or sends a message by itself.
- OAuth sign-in uses PKCE and a single-use, expiring state value.
- OAuth client secrets, tokens, and API keys are stored encrypted and never displayed.

Connectors change the risk picture in these ways:

- **Data leaves the installation.** Each connector sends data to a provider the firm chooses. The connect screen lists what is sent and received, and a superuser must acknowledge it. The firm remains responsible for evaluating each provider and its data-use terms. Consumer and business AI plans often have different terms.
- **Some features need public reachability.** Provider webhooks and the MCP endpoint must be reachable from the provider's network. Expose only those paths through the reverse proxy, use polling where a provider supports it, or leave those connectors off.
- **More credentials to protect.** Connection credentials are stored encrypted, scoped to the capabilities the firm turns on, and deleted on disconnect. Where the provider supports it, disconnecting also revokes them there. They use the same `FIELD_ENCRYPTION_KEYS` encryption as two-factor keys.
- **Outbound actions are gated.** Sending messages, taking payments, writing calendar events, calling AI models, legal research searches, and sending documents for signature pass an outbound policy and start in preview mode. Calendar events the CRM writes have no attendees, so no invitations go out. A research search sends only the words staff type, and the search box asks them to describe the legal issue rather than the client.
- **Webhooks fail closed.** A webhook with a missing or invalid signature is rejected in every environment, including development settings. Each connection has its own unguessable URL token, stored only as a hash. Twilio's connector, which needs the address to ask for delivery updates, keeps it encrypted with its other credentials. Dialpad's events are signed tokens (JWT, HS256 only), read only after their signature checks out. Twilio's are checked against the exact address and every parameter. Calendly's signature covers a timestamp, and deliveries more than three minutes off are refused, so a captured one cannot be replayed later. Microsoft Graph's carry a shared value (`clientState`) instead of a signature. They are accepted only with that value and a known subscription ID, and the CRM fetches what changed from Graph itself, never trusting the notification's contents. LawPay does not sign its webhooks at all, so a LawPay delivery is only a hint: the CRM fetches the event it names from LawPay with the firm's key and records only that, and payments also arrive by polling. A verified event's payload is stored minimized and encrypted, and deleted once the worker has processed it.

- **Signed documents come from the provider.** A document sent for signature goes only to the matter's own contacts, at their email addresses on file. When the e-signature service reports it signed, the connector fetches the signed PDF from the service's API rather than taking anything from the webhook, and the CRM files it only if it is a PDF, only for a request it sent, and only once.

## AI features

The CRM's own AI features send data to the AI model the firm connects, so they are guarded the same way as other outbound actions:
- **Off by default.** AI, and each feature, stay off until a superuser turns them on.
- **Through the outbound policy.** Every request starts in preview mode on a new connection, is limited per hour, and is logged with the feature, the person, and the connection that paid. Prompts and answers are not logged.
- **Personal connections stay personal.** A staff member's own AI connection runs only their own requests. Background work runs only through the firm-wide connection a superuser chooses.
- **No subscription credentials.** The Anthropic connector takes only Claude Console API keys and refuses anything else. Anthropic does not permit apps to use Claude.ai credentials. The ChatGPT plan connector is the one subscription path, behind a flag until OpenAI confirms this use. Each staff member signs in to their own plan, which runs only their own requests. The CRM checks the sign-in's ID token against OpenAI's published keys and accepts a pasted sign-in address only for the expected loopback address and a sign-in this CRM started in the last ten minutes.
- **Less kept at providers.** OpenAI requests are sent with `store: false`. OpenRouter requests always deny data collection by the providers it routes to. A model the firm runs itself keeps everything local.
- **Records are data.** Records hold text written by clients and other parties, which could try to instruct the model. Every feature's instructions say to treat records and tool results as data, and tool results are marked as untrusted.
- **People confirm changes.** The assistant changes the CRM only after the staff member confirms each change, and it cannot send emails or texts: it drafts them for staff to send. Task suggestions create nothing until someone accepts them.
- **Same permissions.** The assistant looks things up as the person asking, with their permissions. Each conversation is visible only to its owner.

## AI assistants (MCP)

The MCP server gives an assistant the access of the staff member who approved it, so its defaults are cautious:
- **Off by default.** The server and its write tools are off until a superuser turns them on.
- **Signing in.** Each person signs in with their own account, including the two-factor sign-in the firm requires, and approves each assistant on a consent page that names where it returns.
- **Tokens.** Tokens are bound to `/mcp`, stored only as hashes, last an hour, and renew for up to 30 days. Each one can be disconnected at once.
- **Writes.** Write tools are marked so Claude and ChatGPT ask before each one. Nothing can be deleted.
- **Logs.** Every call is logged without its contents.

The assistant itself, and its vendor's data terms, are outside the CRM's control. Prefer business plans or API access for client information.

## Outbound webhooks

Automation events leave the installation, so the defaults are cautious:
- **Nothing is sent until a superuser chooses events.** New connections start in preview mode, and every delivery passes the outbound policy.
- **IDs and links by default.** Events carry IDs and links. Full records, which can include client details and intake answers, are sent only if a superuser chooses that.
- **Signed.** Each delivery is signed with a per-connection secret, which is stored encrypted.
- **Address rules.** Addresses must use HTTPS except for hosts on the firm's own network, and redirects are not followed.
- **Payloads are deleted.** Queued payloads are encrypted and deleted once each delivery settles.

## API tokens

An API token is a bearer credential that acts as one staff member without two-factor sign-in, so the API's defaults are cautious:
- **Off by default.** The API is off until a superuser turns it on.
- **Superusers only by default.** Only superusers can create tokens unless a superuser allows staff tokens.
- **Limited.** Every token is scoped to the records it needs, expires within a year, and can be revoked at once.
- **Hashed.** Only a SHA-256 hash of each token is stored.
- **Logged.** Every request is logged, without query strings or bodies.
- **Two-factor rule.** When the firm requires two-factor sign-in, tokens of users who have not set it up are refused.

Store tokens in the automation tool's secret store, give each tool its own token, and revoke any token that may have leaked.

## Intake link handling

An intake URL is sensitive. Send it only to its intended recipient, keep expiry short, revoke it if misdirected, and do not place it in analytics or third-party link shorteners. When the CRM emails a link, it goes to the contact's email address on file, so check the address first; the email service the firm connects can see the link while delivering it. The default referrer policy and CSP reduce browser leakage, but the recipient's device and delivery channel remain outside this application's control.
