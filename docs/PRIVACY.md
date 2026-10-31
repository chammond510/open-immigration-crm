# Privacy and data handling

Open Immigration CRM performs no telemetry, analytics, advertising, email delivery, cloud-drive sync, or payment processing, and no AI inference unless the firm connects an AI model and a superuser turns AI features on (see [AI features](AI.md)). Core application behavior has no intentional outbound provider calls. Optional connectors send data only to providers a firm chooses to connect, after a superuser acknowledges what each one sends. For example, the USCIS Case Status connector sends the receipt numbers of open matters to USCIS, and only in production, and a texting connector sends the contact's number and the message for each text staff send. Through WhatsApp Business, Meta's Cloud API handles each message's text and keeps messages for up to 30 days; see the [security model](SECURITY_MODEL.md). Telemetry stays excluded.

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

Superusers can download a limited contacts/matters CSV. Tracking numbers of a firm's shipments go to the carrier whose connection tracks them, and a carrier's own words about a package are cleared from the shipment nine months after its last check. Files clients send with a message, such as photos of notices, are kept in the private media storage on the same terms as uploaded documents, and are deleted with their message. A machine translation that staff ask for is stored beside the original; only that text is sent, to the translation service or AI connection staff choose. A spreadsheet uploaded for [import](SPREADSHEET_IMPORT.md) is stored encrypted for a day and then deleted; the report kept afterwards has row numbers and reasons, not the rows' values. `python manage.py export_portable_data output.json` exports CRM database objects to a new mode-0600 JSON file (POSIX file modes; on other platforms protect the output directory yourself) and refuses to overwrite an existing path; uploaded file bytes must be copied separately. Django admin exposes individual record management, but relational protections may require deleting dependent records in a deliberate order. There is intentionally no bulk-delete button.
