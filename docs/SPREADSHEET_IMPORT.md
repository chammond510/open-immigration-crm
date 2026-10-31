# Importing from a spreadsheet

Superusers can bring contacts and their matters into the CRM from a spreadsheet saved as CSV, such as an export from another practice-management system. Open **Import from a spreadsheet** in the sidebar's account links.

Each row is a contact, with a matter when the row has one. A client with two matters takes two rows.

## Steps

1. **Upload the file.** Save it from the spreadsheet app as CSV UTF-8. It can be up to 5 MB and 5,000 rows, with a header row naming each column. Files saved in the older Windows encoding are read too. Columns may be separated by commas, semicolons, or tabs. **Download the template** gives a header row with every field the CRM can fill, including the firm's custom fields.
2. **Match columns.** The CRM matches each column to a field from its header, such as "Email Address" to Email or "Practice Area" to Case type.
   - Check each match, and leave columns you don't need as **Don't import**.
   - Say how the file writes dates.
   - Choose the status, stage, and assignee for rows that don't give their own.
3. **Preview.** The preview shows every row that will be skipped, with its reason, and the rows that will be imported. Nothing has been saved yet.
4. **Import.** Every accepted row is saved in one step. Each new contact and matter gets a note saying it was imported, and sends the same [webhook events](WEBHOOKS.md) as one added by hand.

## From another system

A migration connector, such as [Clio Manage](../connectors/clio/README.md), brings its records the same way. A superuser chooses **Fetch records to import** on the connection's page, and the records become an import listed there and here, ready to review. Its columns are already matched, and these rules differ:

- Each record is linked to its source when imported, and the next fetch leaves out what was imported before.
- A contact already in the CRM is linked, not skipped, even with no matter to add.
- A matter's other linked contacts are added as its parties, with their role.
- A client's status and stage follow its matters: Client and Active matter while one is open or pending, otherwise Former client and Closed. The defaults chosen for the import apply to other contacts.

## How rows are matched

- **Existing contacts are never changed.** A row whose email or phone matches a contact already in the CRM adds its matter, if it has one, to that contact. A row with no matter is skipped.
- **Repeated rows in the file.** A row whose email or phone matches a row earlier in the file adds its matter to the contact that row adds. A row with no matter is skipped.
- **Rows with no email or phone** cannot be matched, so importing the same file twice adds those people twice. The preview warns when no column fills Email or Phone.
- **Two contacts.** If a row's email and phone match two different contacts, the row is skipped.
- **Receipt numbers.** A matter whose receipt number is already in the CRM, or earlier in the file, is skipped. Spaces and dashes are ignored when comparing.

## What each column accepts

- **Names.** First name and Last name are required for a new contact. A Full name column can stand in for both: its last word becomes the last name.
- **Lists.** Contact status, Stage, Case type, Matter status, and choice custom fields accept a choice's name or its stored value, ignoring case and punctuation. For example, "Family-based" and "family" both fill Case type.
- **Dates.** A date must be a full date, with a four-digit year, written in the order chosen when matching columns. Year-month-day, such as 2026-03-31, is always accepted. A time after the date is ignored.
- **Assigned to.** This takes an active staff member's email or username.
- **Yes or no.** Yes-or-no custom fields take yes, no, true, false, 1, or 0.
- **Everything else** is checked as on the web pages' forms. For example, an email must be a valid address.

## Skipped rows

The preview and the report list each skipped row's number, as the spreadsheet app numbers it with the header as row 1, and why it was skipped. **Download the skipped rows** gives those rows as they were in the file, with the reason in a column of its own. Fix them there and import that file.

## What is kept

The uploaded file is stored encrypted for a day, for the preview and the skipped rows, and then deleted. An import that was never confirmed is deleted with it. **Delete the uploaded file now** on an import's report deletes the file sooner.

The report kept afterwards has the counts, the row numbers, and the reasons. Reasons never repeat a row's values.

Each import is recorded in the audit log, and so is each download of skipped rows.
