# USPS

Tracks the packages a firm mails with USPS, such as filings to USCIS, and notes on the matter when each is delivered or runs into a problem. It is in Beta.

## Before you connect: which packages USPS will track

Since April 2026, USPS shares tracking through its API only for packages labelled under a **Mailer ID** that the firm's USPS business account owns.
- **Shared:** a label the firm prints through its own USPS business account, under its own Mailer ID.
- **Probably not shared:** a label bought at a Post Office counter, or printed through another company's account, which carries someone else's Mailer ID. USPS does not say so directly.
- **When USPS won't share:** the shipment says so, the CRM stops checking it, and staff update it by hand.

If the firm has no Mailer ID, this connection will not be able to track its packages. USPS's API access page explains the rules: https://www.usps.com/business/api-access.htm. Its access-control helpdesk can confirm which of the firm's labels are covered.

## What it does

- **Shipments.** Each matter has a **Shipments** panel. Staff add a package: what was sent, the carrier, the tracking number, where it went, and when.
- **Checking.** About every four hours, until a USPS package is delivered or returned, the background worker asks USPS where it stands. A shipment is checked for up to 60 days after it was sent, with 35 tracking numbers to a request.
- **What the shipment shows.**
  - Its status: sent, in transit, out for delivery, delivered, needs attention, or returned to sender.
  - USPS's one-line summary of the latest event.
  - When it was delivered, or when it is expected.
- **On the matter.**
  - A delivery is noted in the matter's activity.
  - A problem, such as a missed delivery or a bad address, or a return to the sender, is noted too, with a high-priority task for the matter's owner to look into it.

Staff can always update a shipment by hand, mark it delivered, or stop checking it.

## Setting it up

1. **Register an app.** In the USPS Business Portal (https://developers.usps.com/getting-started), sign in with the firm's business account and create an app under **My Apps**. Tracking is part of the default set of APIs. Accept USPS's updated terms for the app if asked.
2. **Connect.** In the CRM, open **Connections → USPS**, enter the app's consumer key and consumer secret, review what the connection sends and receives, and connect. The connection test gets an access token.
3. **Check on a package.** Add a shipment to a matter, then use **Sync now** on the connection's page to check it straight away.

## Limits

USPS limits how many requests each app makes an hour, and asks firms that need more to contact it. Each check sends only the tracking numbers that are due, so most hourly runs send little or nothing.

Built to USPS's Tracking API v3r2 documentation, read on 2026-10-30. It has not been checked against a live USPS account.
