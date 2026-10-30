# SMTP server

Lets the CRM email clients itself, through the firm's mail server or an email delivery service that offers SMTP, such as Postmark, Amazon SES, SendGrid, or Mailgun. It is in Beta: its tests run against a fake mail server that answers like Python's SMTP library expects, but it has not run against a live service yet.

## What it does

- **Secure intake links.** When staff create an intake link, they can have the CRM email it to the contact. The next page still shows the link once, to copy by hand if the email does not go out.
- **Payment plan reminders.** A payment plan can also email the client before each installment, at 9 a.m. on the day its staff reminder is set for. Each installment is emailed once. Nothing is sent once the installment is paid or its due date has passed. Turn this on when creating the plan, or later from the invoice page.
- **What is kept.** Each email sent is logged on the contact, with a summary written by the CRM. The intake link itself is never kept. If an email is not sent, the intake page or the installment's staff reminder says why, so staff can send it themselves.
- **The email.** Plain text, from the firm's address and name, with an optional Reply-To address. It is marked `Auto-Submitted: auto-generated`, so mail servers do not send automatic replies back.
- **Security.** The connection always uses TLS, with the server's certificate checked: STARTTLS (usually port 587) or SSL from the start (usually port 465). Plain, unencrypted connections are not offered.

Every email goes through the outbound policy, like every other send. A new connection starts in preview mode, so nothing is emailed until a superuser turns preview mode off.

## Setting it up

1. In the email service, verify the address or domain the CRM will send from. Services refuse mail from addresses they have not verified.
2. Create SMTP credentials for the CRM (see the table below).
3. In the CRM, open **Connections > SMTP server**. Enter the sign-in name and password, the server, port and security, and the address to send from. Optionally, enter a sender name and the address replies should go to. The CRM signs in to the server to check the details.
4. A superuser reviews what the connection sends and receives before it is used. Turn preview mode off when the firm is ready for the CRM to email clients.

The firm needs only one SMTP connection. If it has more than one, the oldest active one sends.

| Service | Server | Port and security | Sign-in name and password |
|---|---|---|---|
| Postmark | `smtp.postmarkapp.com` | 587, STARTTLS | The server's API token as both, or an SMTP token's access key and secret key. Turn SMTP on in the server's settings. |
| Amazon SES | `email-smtp.<region>.amazonaws.com` | 587, STARTTLS, or 465, SSL | SES SMTP credentials, which are not the same as IAM access keys. A new SES account is in the sandbox and can email only verified addresses. |
| SendGrid | `smtp.sendgrid.net` | 587, STARTTLS, or 465, SSL | `apikey`, and an API key with Mail Send permission. |
| Mailgun | `smtp.mailgun.org`, or `smtp.eu.mailgun.org` for the EU | 587, STARTTLS, or 465, SSL | The domain's SMTP credentials, from its domain settings. |
| Microsoft 365 | `smtp.office365.com` | 587, STARTTLS | A licensed mailbox's address and password, with SMTP AUTH turned on for it. Microsoft turns this sign-in method off by default at the end of December 2026; after that, use an email delivery service instead. |
| Google Workspace | `smtp.gmail.com` | 587, STARTTLS, or 465, SSL | The address and an app password, which needs 2-Step Verification. Limited to 2,000 messages a day. |

These details come from each service's documentation, checked on 2026-10-30. Services change them, so check yours.

## Before relying on it

Send an intake link to a staff member's own contact record first, with preview mode off, and check that it arrives, is not marked as spam, and that replies reach the right inbox. The service's own setup (SPF and DKIM records for the sending domain) decides whether email is delivered; the CRM cannot check it.
