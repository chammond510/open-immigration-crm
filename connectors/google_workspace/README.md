# Google Workspace

Connects each staff member's Gmail and Google Calendar. Their email with clients is filed on the right contact, they can send email from contact and matter pages, and their CRM appointments and Google Calendar stay in step. It is in Beta: its tests run against a fake Google that follows Google's published APIs, but it has not run against a live account yet.

Each staff member connects their own account; nobody else can send from it. To bring in a shared mailbox, a staff member who uses it connects it as one more connection of their own.

## What it does

### Mail

- **What comes in.** The first sync brings in the last 30 days of the inbox and sent mail. Later syncs, every 10 minutes, follow Gmail's history from where the last one stopped.
- **What is read.** Only each message's headers and Gmail's short snippet. Full messages and attachments stay in Gmail.
- **What is left out.**
  - spam, drafts, and trash;
  - Gmail's Promotions, Social, Updates, and Forums tabs;
  - mailing lists and automated mail;
  - email between staff members.
- **Matching.** An email with a contact is filed on that contact. The connection setting **Email from people who are not contacts** either keeps the rest in **Needs matching**, to link or add as a contact, or leaves it out of the CRM.
- **Sending.** The **Send an email** form sends a plain-text email from the staff member's own Gmail, through the outbound policy. Gmail's copy in Sent Mail lands on the same record when it syncs.

### Calendar

- **Calendar to CRM.** Events on the primary calendar are pulled every 10 minutes, from 30 days back. An event with a contact among its attendees becomes an appointment on that contact. Personal events without contacts stay out of the CRM.
- **CRM to calendar.** Appointments assigned to the staff member are put on their calendar, with the contact and matter in the description. The event has no attendees, and the CRM asks Google to email nobody. Moving the appointment moves the event, and moving the event moves the appointment. Deleting the appointment, or giving it to someone else, removes the event.
- **Events from the calendar stay the calendar's.** An appointment that came from a calendar is updated from it, but never written back. Its attendees are never changed without notice.
- **Preview mode applies.** Like every outbound action, calendar writes start in preview mode: recorded, not sent, until someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) turns preview mode off on the connection.

## Setting it up

### 1. The firm's Google Cloud app (once, by a Google Workspace admin)

1. In the Google Cloud console, create a project that belongs to the firm's Google Workspace organization.
2. Enable the **Gmail API** and the **Google Calendar API**.
3. Open **Google Auth platform > Branding** and start the setup. Under **Audience**, choose **Internal**.
   - If Internal is not offered, the project is not in the firm's organization; check **IAM & admin > Settings**.
   - Internal matters. Gmail read access (`gmail.readonly`) is a restricted scope, and `gmail.send` and Calendar access are sensitive. Google does not require verification for an app used only inside the firm's own organization.
   - An external app in Testing gets sign-ins that expire after 7 days. Published, it would need Google's verification and security assessment.
4. Under **Clients**, create an OAuth client of type **Web application**. As its redirect URI, add the address the CRM shows on the Google Workspace connector's page.
5. In the CRM, someone who manages connections and AI enters the client ID and secret on the Google Workspace connector's page.

Google notes that restricted Gmail scopes can need extra configuration by the organization's administrators. If staff see that the app is blocked, a Workspace admin can mark it as trusted in the Admin console's API controls.

### 2. Each staff member

1. On **Connections**, open **Google Workspace** and connect, choosing mail, calendar, or both.
2. Sign in with Google and allow everything asked for.
3. Someone who manages connections and AI reviews what the connection sends and receives. When the firm is ready, they turn preview mode off so sending and calendar writes go through.

A staff member's sign-in stops working if they change their Google password, which ends Gmail access, or if it goes unused for six months. The connection then asks them to sign in again.

## Limits

Gmail allows 6,000 quota units a minute per user, and reading one message costs 20. The connector spaces its requests to stay under that; a sync reads at most 2,000 messages. Google Calendar allows 600 requests a minute per user.

The steps and limits above are from Google's documentation as of October 30, 2026.
