# Microsoft 365

Connects each staff member's Outlook mail and calendar through Microsoft Graph. Their email with clients is filed on the right contact, they can send email from contact and matter pages, and their CRM appointments and Outlook calendar stay in step. One sign-in serves both mail and calendar. It is in Beta: its tests run against a fake Microsoft Graph that follows Microsoft's published API, but it has not run against a live tenant yet.

Each staff member connects their own account; nobody else can send from it.

## What it does

### Mail

- **What comes in.** The inbox and Sent Items are followed with Graph's delta queries. The first sync looks back 30 days, up to 5,000 messages; later syncs bring only what changed.
- **What is read.** Only the subject, Outlook's 255-character preview, the people, and the times. Full messages and attachments stay in Outlook.
- **What is left out.**
  - drafts;
  - mail Outlook sorts into "Other" rather than Focused;
  - email between staff members.
- **Matching.** An email with a contact is filed on that contact. The connection setting **Email from people who are not contacts** either keeps the rest in **Needs matching** or leaves it out.
- **Sending.** The **Send an email** form sends a plain-text email through the outbound policy. The CRM creates a draft and then sends it, using Graph's immutable IDs, so the copy in Sent Items keeps the same ID and lands on the same record.

### Calendar

- **Calendar to CRM.** Events on the primary calendar are followed with a calendar-view delta from 30 days back to a year ahead; the window moves forward every 30 days. An event with a contact among its attendees becomes an appointment on that contact.
- **CRM to calendar.** Appointments assigned to the staff member are put on their calendar with no attendees, so Outlook sends no invitations. Moving the appointment moves the event, and moving the event moves the appointment. Deleting the appointment, or giving it to someone else, removes the event.
- **Events from the calendar stay the calendar's.** An appointment that came from the calendar is updated from it, but never written back.

### Change notifications

- **Optional.** When webhooks are turned on with **Ask Microsoft Graph to notify this CRM** ticked, Graph tells the CRM about new mail and calendar changes as they happen. Without them, mail and calendar still sync every 10 minutes.
- **How notifications are checked.** Graph proves each notification with a secret value (`clientState`) rather than a signature. The CRM accepts a notification only when that value and the subscription ID match, and then fetches what changed from Graph itself; nothing in the notification is trusted.
- **Renewal.** Subscriptions last two days, and the worker renews them a day before they lapse. One that lapsed anyway is made again.
- **On disconnect.** Disconnecting deletes the subscriptions and the stored sign-in. Microsoft has no way to revoke a single sign-in.

## Setting it up

### 1. The firm's app registration (once, by a Microsoft 365 administrator)

1. In the Microsoft Entra admin center, open **App registrations > New registration**.
2. Choose **Accounts in this organizational directory only**.
3. Add a **Web** redirect URI: the address the CRM shows on the Microsoft 365 connector's page.
4. Under **Certificates & secrets**, create a client secret and copy its value.
5. Under **API permissions**, add these Microsoft Graph delegated permissions:
   - `User.Read`
   - `Mail.Read`
   - `Mail.Send`
   - `Calendars.ReadWrite`
   - `offline_access`

   None needs admin consent by default. If the firm's consent settings let users consent only to low-impact permissions, grant consent for the organization on the same page.
6. In the CRM, a superuser enters the application (client) ID, the client secret, and the **Directory (tenant) ID** on the Microsoft 365 connector's page. Find the tenant ID on the registration's overview page.

   A single-tenant app has to use its own tenant's sign-in address. Microsoft refuses the shared ones for apps registered this way.

### 2. Each staff member

1. On **Connections**, open **Microsoft 365** and connect, choosing mail, calendar, or both.
2. Sign in with Microsoft and accept the permissions.
3. To get changes as they happen, turn on webhooks on the connection's page. The CRM's `/hooks/` address must be reachable from Microsoft. It must answer Graph's validation check within 10 seconds and each notification within 3.
4. A superuser reviews what the connection sends and receives. When the firm is ready, they turn preview mode off so sending and calendar writes go through.

A sign-in stays valid while it is used. Microsoft ends it after 90 days without use, or when an administrator resets the person's password or revokes their sessions. The connection then asks them to sign in again.

## Limits

Microsoft Graph allows 10,000 requests per mailbox in 10 minutes, and four at a time. When it answers 429, the CRM waits as long as `Retry-After` asks.

The steps and limits above are from Microsoft's documentation as of October 30, 2026.
