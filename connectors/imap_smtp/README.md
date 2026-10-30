# IMAP and SMTP email

Connects a staff member's own mailbox at almost any mail provider. Email with clients is filed on the right contact, and the staff member can send email from contact and matter pages. It is in Beta: its tests run against a fake mail server that answers like Python's IMAP and SMTP libraries expect, but it has not run against a live mailbox yet.

For Gmail or Outlook, use the **Google Workspace** or **Microsoft 365** connector instead; they sign in without an app password.

## What it does

- **Reading mail.** The first sync brings in the last 30 days of the inbox and the sent folder. Later syncs continue from the last message seen, about every 15 minutes. Messages are read without marking them as read.
- **What is kept.** Only the sender, recipients, subject, time, and a short plain-text excerpt. The excerpt comes from messages under 256 KB; bigger ones keep headers only. Attachments are never stored. Mailing lists and automated mail are skipped. Email from people who are not contacts waits in **Needs matching**, unless the connection is set to leave it out.
- **Sending.** Email sent from a contact or matter page goes out through the provider's SMTP server as plain text, through the outbound policy. A copy is put in the sent folder, unless the provider keeps one itself; when it does, set **Keep a copy of sent email** to no to avoid two copies.
- **One record per email.** An email is filed under its Message-ID, so an email sent from the CRM and its copy in the sent folder land on the same record.
- **Security.** Both connections always use TLS, with the server's certificate checked. Plain, unencrypted connections are not offered.

## Setting it up

1. In your mail provider's security settings, create an **app password** for this CRM. Your usual password is not used, and two-step sign-in can stay on. Most providers that support IMAP offer app passwords; some call them app-specific or device passwords.
2. Find your provider's IMAP and SMTP server names in its help pages. IMAP is usually port 993 with SSL. SMTP is usually port 465 with SSL, or port 587 with STARTTLS.
3. In the CRM, open **Connections > IMAP and SMTP email**, enter your address, the app password, and the server details, and connect. The CRM signs in to both servers to check them.
4. A superuser reviews what the connection sends and receives before it is used. A new connection starts in preview mode, so no email is sent until preview mode is turned off.
5. If your sent folder has an unusual name, enter it under **Sent folder**. Otherwise the folder your server marks as Sent is used.

Each staff member connects their own mailbox; nobody else can send from it.
