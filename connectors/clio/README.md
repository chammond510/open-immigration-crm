# Clio Manage

Moves the firm's contacts and matters over from Clio Manage. It only reads from Clio: nothing there is changed. Each fetch becomes an import a superuser reviews before anything is saved, and a later fetch brings only what is new. It is in Beta.

## What it brings

- **Contacts:** each person's name, primary email and phone, date of birth, and primary address.
  - Clio companies are listed as skipped: the CRM's contacts are people, so add any a firm needs by hand.
- **Matters:** the first line of the description becomes the title, falling back to the matter number. The rest of the description is kept, with the matter number, the open date, and the practice area.
  - **Status:** Open, Pending, and Closed carry over.
  - **Case type:** the practice area's name suggests one, such as Family or Asylum; otherwise it is Other.
  - **Responsible attorney:** if that person's email belongs to an active staff member here, the matter is assigned to them.
- **Links:** each matter's client, and the other contacts Clio links to the matter, which become its parties with Clio's description of their role.

Only a matter's primary client is brought; Clio's other clients of a matter are not. Custom fields, notes, documents, bills, and time entries are not brought.

## Setting it up

1. **Register a private app in Clio.** Use the developer portal for the firm's region: [United States](https://developers.clio.com), [Canada](https://ca.developers.clio.com), [Europe](https://eu.developers.clio.com), or [Australia](https://au.developers.clio.com).
   - Give it read permission for **Contacts** and **Matters**, and nothing else.
   - For the redirect URI, use the address the CRM shows on **Connections → Clio Manage**.
   - Clio does not review private apps. Registering one means accepting Clio's Developer Terms of Service; read them first, and decide whether they allow the firm's use. This guide is not legal advice.
2. **In the CRM,** a superuser enters the app's ID and secret on **Connections → Clio Manage**, and chooses the region.
3. **Connect and sign in** with a Clio user who can see every contact and matter to bring. Clio's contact visibility settings can hide some from some users.
4. **Review what the connection receives,** then use **Test connection**.

## Importing

1. On the connection's page, choose **Fetch records to import**. The background worker reads every contact and matter, spacing requests to stay inside Clio's limit of 50 a minute at busy times, so a large firm's fetch takes a few minutes.
2. When it finishes, the connection's page lists the import as ready to review. Open it to see what each row would do. Every row is checked as for a [spreadsheet import](../../docs/SPREADSHEET_IMPORT.md):
   - A contact already in the CRM, matched by email or phone, is linked and never changed.
   - A row that fails the CRM's checks, such as a contact with no last name, is skipped with the reason. Fix it in Clio and fetch again.
3. Confirm. Each contact and matter saved is linked to its Clio record, so the next fetch leaves it out.

Keep one Clio connection for the whole move. A fetch leaves out what that connection imported before. A new connection would not know what was imported, so its matters would come in again; contacts would still be matched by email or phone. If the sign-in stops working, use **Reconnect** on the same connection. Disconnecting ends the sign-in at Clio.

## Not checked against a live account

These follow Clio's documentation, read on 2026-10-30, and the tests use a fake Clio built from it:

- the regional sign-in addresses, such as `eu.app.clio.com/oauth/authorize`, which Clio documents for the United States only;
- whether Clio accepts a redirect URI on `http://localhost` while testing.
