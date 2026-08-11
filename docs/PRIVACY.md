# Privacy and data handling

Open Immigration CRM performs no telemetry, analytics, advertising, AI inference, email delivery, cloud-drive sync, or payment processing. Core application behavior has no intentional outbound provider calls.

## Data locations

- PostgreSQL stores users, firm settings, contacts, matters, notes, work, checklists, document metadata, intake records, and audit events.
- The private media volume stores uploaded file bytes.
- Operator-created exports and backups contain confidential data and must never be committed to source control.
- Reverse proxies and hosting platforms may create access/error logs. Configure their content, access, encryption, and retention separately.

## Operator role

Each installation operator determines what is collected, why it is used, who has access, how long it is retained, and how access/correction/deletion requests are handled. The project does not provide compliance certification or legal advice. Operators should assess professional-conduct, privacy, breach-notification, cross-border, records-retention, and client-consent requirements that apply to them.

## Data minimization

Collect only what the firm needs. Avoid placing full client stories or document contents in titles, URLs, filenames, and infrastructure logs. Use short intake-link expirations. Review staff access regularly. Delete abandoned exports promptly and securely under firm policy.

## Exports and deletion

Superusers can download a limited contacts/matters CSV. `python manage.py export_portable_data output.json` exports CRM database objects to a new mode-0600 JSON file (POSIX file modes; on other platforms protect the output directory yourself) and refuses to overwrite an existing path; uploaded file bytes must be copied separately. Django admin exposes individual record management, but relational protections may require deleting dependent records in a deliberate order. There is intentionally no bulk-delete button.
