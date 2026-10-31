# Documenso

Sends a matter's documents for signature through Documenso, either Documenso's hosted service or the firm's own Documenso, and files the signed copy on the matter. It is in Beta: its tests run against a fake Documenso that answers as Documenso's API v2 documentation describes, but it has not run against a live Documenso yet.

## What it does

- **Sending.** On a matter page, under **Signatures**, staff choose one of the matter's PDFs and the signers: the matter's primary contact and parties who have an email address. The CRM uploads the PDF to Documenso with each signer and a signature field for each, near the bottom of the last page, and sends it. Documenso emails the signers.
- **Progress.** The Signatures panel shows each request as out for signature, signed, declined, or cancelled, and who has signed so far.
- **The signed copy.** When everyone has signed, the CRM downloads the signed PDF from Documenso and files it with the matter's documents, named after the request, with "(signed)" added.
- **Cancelling.** Staff can cancel a request that is still out for signature; it is cancelled in Documenso too.
- **What leaves the CRM.** Only the PDF chosen, its title, the message, and each signer's name and email.

Sending goes through the outbound policy, like every other send. A new connection starts in preview mode, so nothing is sent until preview mode is turned off.

## Setting it up

1. In Documenso, open the team's settings, then **API tokens**, and create a token for the CRM. It starts with `api_`. Tokens belong to a team and act with the role of the person who made them, so make it as a team admin or manager. If it is set to expire, note the date and connect again with a new token before then.
2. In the CRM, a superuser opens **Connections > Documenso**, enters the token, and, for the firm's own Documenso, its address, such as `https://sign.example.test`. Plain HTTP is accepted only on this computer or the firm's network. The CRM checks the token.
3. On the connection's page, turn on webhooks. Copy the address and the secret the CRM shows.
4. In Documenso, open the team's settings, then **Webhooks**, add the address, paste the secret, and choose the events **Document signed**, **Document completed**, **Document rejected**, and **Document cancelled**. Webhooks are available for teams.
5. A superuser reviews what the connection sends and receives, and turns preview mode off.

## How deliveries are checked

Documenso does not sign its webhooks; it sends the secret itself in the `X-Documenso-Secret` header. The CRM refuses any delivery without the right secret, and even then it trusts the delivery for nothing but which document it is about: it asks Documenso's API where that document stands, and downloads the signed PDF from the API. A copy of an old delivery therefore cannot mark a document signed. Documenso may deliver an event more than once; each is filed once.

For a self-hosted Documenso on the firm's own network, Documenso blocks webhook addresses that resolve to private addresses unless they are listed in its `NEXT_PRIVATE_WEBHOOK_SSRF_BYPASS_HOSTS` setting.

## Limits

- PDFs only, up to 50 MB. Password-protected PDFs are refused by Documenso.
- Documenso's free plan allows 5 documents a month; Documenso refuses more.
- Documenso's API v1 is deprecated and ends on March 1, 2027; this connector uses API v2.
