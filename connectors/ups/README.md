# UPS

Tracks the packages a firm ships with UPS, such as filings to USCIS, and notes on the matter when each is delivered or runs into a problem. It is in Beta.

## What it does

The connection tracks shipments in a matter's **Shipments** panel (see [the USPS guide](../usps/README.md#what-it-does) for how shipments work).

- **Checking.** About every four hours, until a UPS package is delivered or returned, the worker asks UPS where it stands.
  - UPS's API takes one tracking number at a time, so each hourly run asks about at most 40, spaced a second apart.
  - That stays inside the free tier's monthly quota, which UPS gives as 40,000 or 90,000 tracking calls depending on the page.
- **What the shipment shows.** Its status (sent, in transit, out for delivery, delivered, needs attention, or returned to sender), UPS's description and where the latest scan was, and when it was delivered or is scheduled.
- **On the matter.** A delivery is noted on the matter. A problem or a return is noted with a high-priority task for the matter's owner.

UPS shares status for any UPS tracking number. It shares delivery details, such as who signed, only with a party to the package, such as its shipper.

## What UPS's agreement asks

The UPS API Access Agreement limits keeping UPS tracking data to nine months after each request.
- **Cleared after nine months:** the CRM clears a carrier's own words from a shipment nine months after its last check.
- **Kept:** the shipment's status and delivery date, which are the firm's own record.
- **Notes and tasks** never copy UPS's text.

The agreement also limits tracking to the firm's own shipments, and has terms about third-party platforms and distribution. Read it before connecting; this guide is not legal advice.

## Setting it up

1. **Create an app.** In the UPS Developer Portal (https://developer.ups.com/get-started), sign in with the firm's UPS profile and add an app.
   - Tie it to the firm's UPS shipper account, and accept the API Access Agreement.
   - Choose the Community tier.
   - Add the **Tracking** product.
2. **Connect.** In the CRM, open **Connections → UPS**, enter the app's client ID and secret, review what the connection sends and receives, and connect. The connection test gets an access token.
3. **Check on a package.** Add a UPS shipment to a matter, then use **Sync now** on the connection's page.

Built to UPS's Tracking API documentation, read on 2026-10-31. It has not been checked against a live UPS account.
