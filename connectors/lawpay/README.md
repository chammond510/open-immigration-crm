# LawPay

Sends clients payment links for an invoice and records LawPay payments and refunds on the invoice. Advances are paid into trust and earned fees into operating. LawPay is run by 8am, formerly AffiniPay. This connector is in Beta: its tests run against a fake LawPay that follows the public 8am API reference, but it has not run against a live account yet.

## What it does

- **Payment links.** On an issued invoice, **Create payment links** makes one link per account still owed. An invoice with both advances and earned fees gets two links, so each part is paid into the right account.
  - Each link is the firm's LawPay hosted payment page for that account, prefilled with the amount, the invoice number as the reference, and the client's name and email.
  - The amount and reference are read-only, so the client can't change them.
  - Links are kept on the invoice for staff to copy and send. Like every outbound action, they wait while the connection is in preview mode.
- **Payments.** Every 10 minutes the CRM reads LawPay's event list, which is what the 8am documentation recommends.
  - A completed charge whose reference is an invoice number is recorded on that invoice, once, in the account it was paid into.
  - A refund of the whole charge voids the payment.
  - A partial refund is noted for staff, who void the payment and record what was kept.
  - The first check starts from the moment of connecting, so payments recorded by hand before then are not doubled.
- **Operating or trust.** Each charge says which LawPay account received it. On the connection's page, the firm ticks which LawPay accounts are trust and which are operating. Unticked accounts follow LawPay's own trust flag. A payment into an account that is neither is never guessed when the invoice has advances; it waits for staff.

## Setting it up

1. In LawPay, a user with the Administrator or Developer role opens **Settings > Integrations > Add New Application > My Website** (or **Developers > Show Credentials**) and copies the live **secret key**.
2. In the CRM, someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) opens **Connections > LawPay**, pastes the secret key, reviews what the connection sends and receives, and connects.
3. In LawPay, on the **New Charge** page, choose the operating account, then **Actions > Show & Copy Page Link**. Do the same for the trust (IOLTA) account.
4. On the connection's page, under **Settings**:
   - paste both page addresses;
   - tick which LawPay accounts are trust and which are operating;
   - save.
5. When the firm is ready, someone who manages connections and AI turns off preview mode so links can be made.

### Webhooks (optional)

LawPay's webhooks are **not signed**; 8am's documentation says anyone can send data to a webhook address. So the CRM treats a delivery only as a prompt to check sooner:

- it reads the event ID;
- it fetches that event from LawPay with the firm's key;
- it records only what LawPay itself returns.

A forged delivery can at most make the CRM look up a real event. To use them, turn on webhooks on the connection's page and paste the address into LawPay under **Settings > Integrations > Add New Webhook URL**. The signing secret the CRM shows is not used. Without webhooks, payments still arrive within about 10 minutes.

## To confirm on a live account

The public documentation leaves these open:

- **The amount format** on the hosted payment page. The CRM sends dollars and cents, such as `1500.00`.
- **The reference.** That the page's `reference` becomes the charge's reference, which is how a payment finds its invoice.
- **Rate limits.** The API's are not published.

If a test payment does not land on its invoice, check the charge's reference in LawPay. The CRM's audit log records every payment it could not place.

The steps above are from 8am's developer documentation (developers.8am.com) and LawPay's help center as of October 30, 2026.
