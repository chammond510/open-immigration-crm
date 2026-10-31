# WhatsApp Business

Sends WhatsApp messages to clients from contact and matter pages through the firm's own WhatsApp Business Platform number, on Meta's Cloud API. Clients' replies and delivery updates are filed in the CRM automatically. It is in Beta: its tests run against a fake Graph API that follows Meta's published documentation (read 2026-10-31), but it has not run against a live account yet.

Each firm connects its own Meta app, WhatsApp Business Account, and number. The project runs no service and holds no Meta app of its own.

## What it does

- **Sending.** The contact or matter page has a **Send a text** form. WhatsApp allows free-form messages only within 24 hours of the client's last message, its customer service window. While the window is open, the page says until when and the form works as for any text. Otherwise the page offers **Send an approved template** instead.
- **Templates.** The template page lists the account's approved templates, shows each one's text, and asks for its values, such as the client's first name. Templates with a photo, video, document, or location header, a link that takes a value, or a one-time code are left out, since they need more than words. A sent template is logged as its text with the values in place. The CRM keeps the list for five minutes, since Meta limits how often it can be read.
- **Receiving.** Messages to the firm's number are filed on the contact with that number; a message from a number no contact has waits in **Needs matching**. Photos, documents, voice messages, and videos are not downloaded. The message notes what was sent, with its caption, so staff can ask for the file another way. Reactions are not filed.
- **Usernames.** A client who uses a WhatsApp username may arrive without their number. Their message is filed as from "WhatsApp user" and the username, and waits in Needs matching. Without a number it does not open the 24-hour window on the contact's page, so the reply is a template to the number on the contact.
- **Opt-outs.** A reply of STOP (or STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT) records that the contact opted out of texts, and START or UNSTOP records consent again, as for other texting services. The opt-out covers every texting connection. A client can also stop marketing messages in WhatsApp itself; Meta then refuses marketing templates to them.
- **Delivery updates.** Messages sent from the CRM show Sent, then Delivered, Read, or Failed. A status never moves backward when updates arrive out of order.

## Before you start, at Meta

1. A Meta business portfolio and a Meta app with the WhatsApp product, for this CRM alone. Messages for other numbers on the app are ignored, so use a separate app for each number the firm connects.
2. The firm's business phone number, registered on the WhatsApp Business Platform. A number registered there can no longer be used in the WhatsApp app on a phone.
3. A payment method on the account. Since October 1, 2026, free-form replies are free for the first 1,000 a month per number, then charged; templates are charged by category. Without a payment method, messages past the free tier are not delivered.
4. A system user in the business portfolio with access to the WhatsApp account, and an access token for it with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions. If Meta offers a token that does not expire, choose it; otherwise replace the token before it expires.
5. Approved templates, written and submitted in WhatsApp Manager. Review takes up to a day. Meta sorts templates into utility, marketing, and authentication, and can move a template to marketing. **WhatsApp does not deliver marketing templates to US numbers**, so a template meant for clients in the US should be a plain utility message about their matter, such as an appointment reminder. Templates may not ask for ID numbers or other sensitive identifiers.

New business portfolios can start conversations with 250 people a day, counting only messages outside the 24-hour window; verifying the business raises it.

## Setting it up

1. In the Meta app, copy the **Phone number ID** and the **WhatsApp Business Account ID** from the WhatsApp **API Setup** page, and the **App secret** from **App settings > Basic**.
2. In the CRM, open **Connections**, then **WhatsApp Business**. Enter the access token, both IDs, and the app secret. A superuser reviews what the connection sends and receives, then connects. The connection test checks the number and that the account's templates can be read.
3. On the connection's page, turn on webhooks. The CRM shows the webhook address and a verify token. In the Meta app's WhatsApp **Configuration** page, enter the address as the callback URL and the verify token, choose **Verify and save**, and subscribe to the **messages** field. Meta checks the address with the token before saving it. If messages still do not arrive, check that the app is subscribed to the WhatsApp Business Account, and that the app is live rather than in development mode.
4. Meta can send up to 3 MB of events at once, and the CRM refuses larger bodies than `CONNECTOR_WEBHOOK_MAX_BYTES` (1 MB by default). Set it to `3145728`, and raise the reverse proxy's limit for `/hooks/` to match.
5. Turn off preview mode on the connection when the firm is ready to send real messages.

The webhook address must be reachable from Meta over HTTPS with a valid certificate; Meta does not accept self-signed ones. Meta signs each delivery with the app secret, so if the app secret changes, connect again with the new one and turn webhooks on again.

## For the firm's lawyer

These points come from Meta's terms and policies, read on 2026-10-31. They are for the firm to weigh; the project gives no legal advice.

- **Opt-in.** Meta's Business Messaging Policy requires the client's opt-in, naming the firm, before the firm messages them, and that the firm respect a request to stop made on or off WhatsApp. An engagement letter or intake form can carry it.
- **Confidentiality.** Meta's Cloud API reads and handles each message's text, so WhatsApp does not treat these chats as end-to-end encrypted, and it keeps messages for up to 30 days. Meta's terms for the platform disclaim fitness for businesses with heightened confidentiality needs, naming legal services, and ask businesses not to give Meta data the law requires them to safeguard. Meta's No Storage and Local Storage options must be chosen before the number is registered.
- **Immigration status.** Meta's Platform Terms forbid using platform data to make eligibility decisions about people, and name immigration status among them. Whether that reaches a firm's own case work is for the firm's lawyer.
- **Sharing and AI.** Meta's terms limit sharing WhatsApp data with third parties and forbid using it to train AI models. The CRM's AI and translation features are off unless a superuser turns them on. A firm that turns them on should decide whether WhatsApp messages may be sent to the AI or translation service it connected.
- **Deletion.** Meta's terms ask businesses to delete platform data when a client asks, or when it is no longer needed. That should be squared with the firm's duty to keep client files.

## What it never does

- It never answers a message on its own.
- It never downloads attachments, and never sends one.
- It never puts message text or phone numbers in the errors it records. A refusal is explained by Meta's error code.
