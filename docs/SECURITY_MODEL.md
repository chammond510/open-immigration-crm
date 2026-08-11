# Security model

Read this before storing real client information.

## Protected assets

The system can hold names, contact information, birth dates, addresses, nationality and immigration information, A-numbers, receipt numbers, attorney notes, deadlines, intake answers, and uploaded documents. Treat the entire database, media volume, logs, exports, and backups as confidential.

## Controls provided by the application

- Staff authentication through Django's mature session framework
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
- **No built-in MFA or SSO.** Put the application behind an identity-aware proxy, VPN with MFA, or another access layer that meets your firm's needs.
- **No field-level encryption.** Use encrypted disks/volumes, encrypted database service storage, TLS to the database, and encrypted backups.
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
5. MFA at the access layer and individual named staff accounts.
6. Prompt OS, container, Python dependency, PostgreSQL, and application patching.
7. Encrypted, tested, offsite backups of both database and media files.
8. Monitoring for authentication anomalies, server errors, disk capacity, backup failure, and certificate expiry.
9. A documented offboarding process that disables staff accounts immediately.
10. A tested incident-response plan appropriate for attorney/client information.

## Intake link handling

An intake URL is sensitive. Send it only to its intended recipient, keep expiry short, revoke it if misdirected, and do not place it in analytics or third-party link shorteners. The default referrer policy and CSP reduce browser leakage, but the recipient's device and delivery channel remain outside this application's control.
