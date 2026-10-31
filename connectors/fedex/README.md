# FedEx

Tracks the packages a firm ships with FedEx, such as filings to USCIS, and notes on the matter when each is delivered or runs into a problem. It is in Beta.

## What it does

The connection tracks shipments in a matter's **Shipments** panel (see [the USPS guide](../usps/README.md#what-it-does) for how shipments work).

- **Checking.** About every four hours, until a FedEx package is delivered or returned, the worker asks FedEx where it stands, 30 tracking numbers to a request. FedEx's default quota is 100,000 tracking requests a day, far more than a firm needs.
- **What the shipment shows.**
  - Its status: sent, in transit, out for delivery, delivered, needs attention, or returned to sender.
  - FedEx's description of the latest scan, where it was, and any reason FedEx gives for an exception.
  - When it was delivered or is expected.
- **On the matter.** A delivery is noted on the matter. A problem, such as a delivery exception, or a return is noted with a high-priority task for the matter's owner.
- **Exceptions.**
  - A new tracking number can take up to a day to appear, and until then the shipment says the carrier has no record of it yet.
  - Packages sent with FedEx Authenticated Delivery are not shared through this API, so they are kept by hand.

## Setting it up

1. **Create a project.** In the FedEx Developer Portal (https://developer.fedex.com/api/en-us/get-started/shipper.html), sign in with the firm's FedEx user ID.
   - Create an organization and add the firm's FedEx account to it.
   - Create an API project with **Basic Integrated Visibility** (tracking).
   - Assign the firm's account to the project to get its production API key and secret key. FedEx shows the secret key only once.
2. **Connect.** In the CRM, open **Connections → FedEx**, enter the project's API key and secret key, review what the connection sends and receives, and connect. The connection test gets an access token.
3. **Check on a package.** Add a FedEx shipment to a matter, then use **Sync now** on the connection's page.

FedEx's developer license covers tracking the firm's own shipments, and separates shippers from providers who build software for other companies. A firm connecting its own project for its own packages fits the shipper path. Read FedEx's terms before connecting; this guide is not legal advice.

Built to FedEx's Basic Integrated Visibility (Track API) documentation, read on 2026-10-31. It has not been checked against a live FedEx account.
