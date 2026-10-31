# Dialpad

Files the firm's Dialpad calls and texts on the right contact automatically, with recording links and Dialpad's AI recap. Staff can send texts from contact and matter pages. It is in Beta: its tests run against a fake Dialpad that follows Dialpad's published API, but it has not run against a live account yet.

## What it does

- **Calls.** Ended calls arrive by webhook as they happen. Each call appears once in the contact's communications, with its direction, time, and length, and any of these Dialpad sends later:
  - the recording link;
  - "voicemail" when the caller left one;
  - Dialpad's AI recap, when the firm's plan includes AI Recaps.

  A call from a number no contact has waits in **Needs matching**. The call is logged as the staff member whose Dialpad line it was, when their CRM email matches.
- **Catching up.** Every hour, the connector also reads Dialpad's call list, starting 30 days back, to file any call the webhook missed.
- **Texts.** Texts in both directions arrive by webhook. STOP and START replies record the contact's opt-out and consent, as for any texting service. Dialpad has no API for past texts, so texts from before the connection are not brought in.
- **Sending texts.** The **Send a text** form sends from the staff member's own Dialpad number, found by their email, or from one firm number set on the connection. Each text goes through the outbound policy, like every outbound action.

## Setting it up

1. A Dialpad company admin creates an API key under **Admin Settings > My Company > Authentication > API Keys**. Give it these scopes:
   - `calls:list`, to catch up on calls;
   - `recordings_export`, for recording links;
   - `message_content_export:all`, for the text of messages.
2. In the CRM, open **Connections**, then **Dialpad**. Paste the API key and choose the environment: production, or Dialpad's sandbox for a test account. To send all texts from one firm number, enter it in **Send texts from**; otherwise leave it blank. Someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) reviews what the connection sends and receives, then connects.
3. On the connection's page, turn on webhooks with **Create the webhook in Dialpad** ticked. The CRM creates a Dialpad webhook signed with a new secret, and company-wide subscriptions to ended calls, recordings, voicemails, AI recaps, and texts. Issuing a new address later replaces that webhook; disconnecting deletes it.
4. Turn off preview mode on the connection when the firm is ready to send real texts.

The webhook address must be reachable from Dialpad. Dialpad sends each event as a token signed with the webhook's secret (a JWT, HS256). The CRM reads the event only from a token whose signature checks out, and refuses anything else.

## Limits

Dialpad allows 20 API requests a second per company, and 100 texts a minute on its basic tier. The connector spaces its API requests under the per-second limit, and the CRM's hourly limit on each connection's outbound actions applies to texts.
