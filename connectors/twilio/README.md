# Twilio

Sends texts from contact and matter pages through the firm's Twilio number or Messaging Service. Incoming texts, delivery updates, and STOP replies are filed in the CRM automatically. It is in Beta: its tests run against a fake Twilio that follows Twilio's published API, but it has not run against a live account yet.

It covers texts only. Calls through Twilio are not connected.

## What it does

- **Sending.** The contact or matter page has a **Send a text** form. Each text goes through the outbound policy. The policy holds it in preview mode and refuses it for a contact who opted out or past the connection's hourly limit, and it records every attempt. A sent text appears in the contact's communications, with its delivery status.
- **Receiving.** Texts to the firm's number are filed on the contact with that number. A text from a number no contact has waits in **Needs matching**.
- **Opt-outs.** A reply of STOP (or STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT) records that the contact opted out of texts, and START or UNSTOP records consent again. With Twilio's Advanced Opt-Out, the CRM also follows Twilio's own reading of the reply. Twilio answers STOP, START, and HELP itself. HELP does not change the contact's opt-out.
- **Delivery updates.** Texts sent from the CRM show Sent, then Delivered, Undelivered, or Failed. A status never moves backward when Twilio's updates arrive out of order.

## Setting it up

1. In the Twilio Console, copy the **Account SID** and **Auth Token** from the account dashboard.
2. In the CRM, open **Connections**, then **Twilio**. Paste both values. In **Send from**, enter the firm's Twilio number in `+12025550143` form, or a Messaging Service SID starting with `MG`. A superuser reviews what the connection sends and receives, then connects. The connection test checks the account and that the number or service belongs to it.
3. On the connection's page, turn on webhooks. Leave **Set this address in Twilio** ticked to have the CRM set the address on the number, or on the Messaging Service. This replaces any address set there now. The CRM then asks for delivery updates on each text it sends.

   To set the address by hand instead, paste it into the number's (or Messaging Service's) incoming-message webhook, using HTTP POST. Texts sent from the CRM then show no delivery updates unless the Messaging Service has a status callback address too.
4. Turn off preview mode on the connection when the firm is ready to send real texts.

The webhook address must be reachable from Twilio. Twilio signs each request with the Auth Token over the exact address it calls, so the CRM must see the same address. Behind a reverse proxy that ends HTTPS, set `TRUST_X_FORWARDED_PROTO=true`. If the Auth Token changes, connect again with the new one.

## What it never does

- It never answers a text on its own. Its webhook replies with empty TwiML, so Twilio sends nothing back except its own STOP, START, and HELP replies.
- It never puts message text or phone numbers in the errors it records. A refusal is explained by Twilio's error code.
