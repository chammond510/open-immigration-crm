# Stripe

Sends clients Stripe payment links for invoices, and records their payments and refunds on the invoice. It is in Beta: its tests run against a fake Stripe that follows Stripe's published API (version `2026-10-28.endive`), but it has not run against a live account yet.

## Trust accounts: read this first

A Stripe account pays out to one bank account, and **Stripe takes its fees out of each payment before paying out**. A Stripe account that pays into the firm's trust account therefore deposits less than the client paid, with the fee taken from client money. Most state bars do not allow that, and some processors built for law firms avoid it by charging fees to the operating account. Check your state's rules, and your Stripe pricing, before choosing **trust** for a Stripe connection. Leave it on **operating** (the default) otherwise, and take advances another way.

## What it does

- **One account per connection.** Each Stripe connection is set to the account its payouts reach. The invoice page asks it for links into that account only, and says when part of an invoice needs another connection or a payment recorded by hand. A firm with a separate Stripe account for trust connects it as a second connection set to trust.
- **Payment links.** Each link is for one invoice and one account, for the amount still owed into it, with the client's email filled in. The payment methods offered (cards, bank debits, and others) are the firm's choice in Stripe's payment method settings.
- **One payment per link.** When a link is paid, the CRM deactivates it in Stripe, so the client cannot pay it twice. If a bank payment later fails, the link is turned back on.
- **Payments.** A card payment is recorded on the invoice when the checkout completes. A bank payment is recorded when it clears, which can take a few days. A full refund voids the payment, and a partial refund is noted for staff, as with other processors.
- **Only the CRM's links.** Payments the firm takes through Stripe in other ways are ignored, and nothing about them is kept.

## Setting it up

1. In Stripe, create a restricted key under **Developers > API keys > Create restricted key**. Give it only these permissions:
   - **Payment Links**: Write
   - **Products**: Write and **Prices**: Write, which Stripe uses for the price on each link
   - **Webhook Endpoints**: Write, only if the CRM should create the webhook endpoint for you

   The account's secret key (`sk_…`) is not accepted. Consider limiting the key to the CRM server's IP address in Stripe.
2. In the CRM, a superuser opens **Connections > Stripe**, pastes the key, reviews what the connection sends and receives, and connects. A new connection starts in preview mode, so no link is made until preview mode is turned off.
3. On the connection's page, set **This Stripe account pays out to**. Read the trust section above before choosing trust.
4. Turn on webhooks with **Create the webhook endpoint in Stripe** ticked. The CRM creates the endpoint for the four events it needs and keeps the signing secret Stripe returns. Issuing a new address later replaces it; disconnecting deletes it.

   To create the endpoint yourself instead, add it in Stripe under **Developers > Webhooks** with the address the CRM shows, for `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, and `charge.refunded`. Then paste its signing secret (`whsec_…`) when turning on webhooks, with the box unticked.

## How deliveries are checked

Each delivery carries a `Stripe-Signature` header with a time and HMAC-SHA256 signatures of that time and the body. The CRM refuses a delivery with no matching signature, or one more than five minutes old, and records each event only once.
