# AI features

The CRM's own AI features use a model the firm connects. Everything is off on a new installation, and core sends nothing to an AI model until a superuser turns AI on and picks the features. This page covers the features, connecting a model, and the controls.

This is separate from [AI assistants (MCP)](MCP.md), which let staff use the CRM from their own Claude or ChatGPT.

## Features

Each feature is off until a superuser ticks it in **AI features**. Whatever a feature writes is labelled as an AI draft for attorney review. The CRM records each feature sends are built in [`crm/ai/records.py`](../crm/ai/records.py). They hold text written by clients and others, so the instructions sent with them say to treat that text as data, never as instructions. The background features and the assistant need the [worker](DEPLOYMENT.md#connector-worker) running.

- **Consult prep memos.** A consult is an open appointment with a contact at the Inquiry or Consultation stage, or one with "consult" in its title.
  - **What it does.** It drafts an internal memo for the attorney: who the person is and what they seem to want, the known facts that matter, questions to ask and documents to request, and what is missing.
  - **What it sends.** The contact's details and internal notes, their intake answers, their matters, open work, and recent communications and notes.
  - **When.** Every 15 minutes, the worker drafts memos for consults in the next two days, through the background connection. It drafts a memo again only when those records change.
  - **Where it shows.** On the contact's page. A staff member can also ask for a memo there, through a connection they may use.
- **Task suggestions.** Every hour, the worker reviews calls, texts, emails, and meetings logged since its last run, grouped by matter, or by contact when there is no matter.
  - **What it sends.** For each group: the new communications, the contact's and matter's details, and their open work. It asks the background connection what the firm needs to do next.
  - **Accepting.** Suggestions appear on the matter, the contact, and the overview of the person they would fall to. Nothing is created until a staff member accepts one. Accepting opens the new-task form with the suggestion filled in, to change before creating it. Staff can also dismiss a suggestion.
  - **No repeats.** A suggestion much like open work, or like one made or dismissed in the last 30 days, is not made again. Suggestions nobody reviews expire after two weeks.
- **Matter assistant.** Staff ask questions on a matter's page, or under **Assistant** in the sidebar. It answers by looking things up in the CRM as the person asking, with their permissions.
  - **Lookups.** The assistant uses the same tools as the [MCP server](MCP.md): searching contacts and matters, matter and contact summaries, upcoming work, USCIS notices, communications, time, the operating summary, and invoices for staff with billing access.
  - **Changes.** It can propose adding a task or note, logging a communication, or marking work done. Each proposal is shown to the staff member, and is made only if they confirm it. The audit log records it as confirmed by staff in the AI assistant.
  - **Drafts.** It can draft an email or text, but it has no way to send one. The draft appears with the usual send form, filled in, and only the staff member can send it.
  - **Conversations.** Each conversation is kept in the CRM, visible only to the person who started it, and they can delete it. Each request sends the conversation so far to the provider.

## Controls

- **Off by default.** In **AI features** (in the sidebar), a superuser turns on AI and each feature separately. While AI is off, no feature runs.
- **Who pays.** Each request runs through one connection, and the log records which.
  - A staff member's request runs through a connection they may use: the firm's, or their own.
  - Work no one is waiting on, such as preparing a memo before a consult, runs only through the firm-wide connection a superuser picks for background work, never a staff member's own.
- **The outbound policy.** Every request passes the same policy as texts and payments:
  - A new connection starts in preview mode, so nothing is sent until a superuser turns preview mode off on the connection's page.
  - A personal connection runs only its owner's own requests.
  - The hourly limit applies.
- **The log.** The AI features page lists recent requests: when, the feature, who asked (or "Background"), the connection, the model, the tokens in and out, and the outcome. The log does not store prompts or answers. What the features produce is kept with the records it belongs to: memos with the appointment, suggestions until they are handled, and assistant conversations until their owner deletes them.
- **A check without client data.** A superuser can send a fixed test message through any AI connection to confirm it works.

## Connecting a model

| Connector | How it connects | Notes |
|---|---|---|
| **Anthropic** | A Claude Console API key (`sk-ant-api…`), and a workspace ID if the key isn't tied to one workspace | Claude.ai subscription sign-ins are refused. Anthropic does not permit apps to use or store Claude.ai credentials. Uses API version `2026-10-21`; the default model is `claude-opus-5-5`. |
| **OpenAI** | An API key and a model ID | Every request is sent with `store: false`, so OpenAI keeps no application state for later retrieval. |
| **OpenRouter** | A superuser signs in at OpenRouter, which hands back a key for the firm's account | Every request asks OpenRouter to route only to providers that don't store or train on data. A stricter setting allows only zero-data-retention providers. Disconnecting deletes the key here; delete it at openrouter.ai/keys too. |
| **OpenAI-compatible endpoint** | A server address and model name, and an API key if the server needs one | Covers models the firm runs itself, such as Ollama (`http://localhost:11434/v1`) or LM Studio. Plain HTTP is allowed only to this computer or the firm's own network. |

Then, in **AI features**, choose the connection for background work, if any. A superuser turns off preview mode on the connection when the firm is ready.

### ChatGPT plan (Beta, behind a flag)

Each staff member can power their own AI requests with their own ChatGPT plan, through Sign in with ChatGPT. It is off unless the installation sets `CONNECTOR_FLAGS=chatgpt_plan`. It stays Beta until OpenAI confirms in writing that a firm-hosted web app used by several staff may use plan usage this way, and its data terms are known.

- **Signing in.** OpenAI sends the sign-in back only to the staff member's own computer (`http://127.0.0.1`), so connecting takes one extra step:
  1. On the connection's page, **Continue with ChatGPT** opens the sign-in in a new tab.
  2. After signing in, that tab shows a page that cannot be reached.
  3. The staff member pastes that tab's address back into the CRM, which finishes the sign-in.
- **Checks.** The CRM checks the sign-in's ID token against OpenAI's published keys, and requires that plan usage was allowed.
- **Their own requests only.** A plan runs only its owner's own requests, never another person's or background work. A superuser still reviews what the connection sends before it is used, as with any connection.
- **Usage.** Wherever staff choose the connection, it is labelled **Using ChatGPT plan**, with a **Manage usage** link to their ChatGPT settings. When the plan's limit is reached, the request stops; OpenAI never bills another way.
- **Requests.** Each request is sent with `store: false`. Disconnecting revokes the sign-in at OpenAI. A staff member can also disconnect the CRM in ChatGPT, under Settings, Security and login, Sign in with ChatGPT.
- **Not yet in place.**
  - OpenAI's ChatGPT logo is not on the button, because OpenAI does not publish the logo file.
  - OpenAI cannot yet revoke plan access for one server alone.
  - OpenAI's documentation names Plus and Pro plans, and does not say whether Business or Enterprise workspaces can use this.

## Data terms

Check each provider's terms before sending client information. The firm remains responsible for that choice. As of October 2026, the providers' own documentation says:

- **Anthropic** does not train on commercial API content. It keeps flagged content longer, and offers zero data retention by arrangement.
- **OpenAI** does not use API data for training unless the firm opts in. It keeps abuse-monitoring logs for 30 days, and zero data retention needs its approval.
- **OpenRouter** stores no prompts or answers by default. The model providers it routes to have their own policies, which is why the CRM always asks it to deny data collection.
- **A ChatGPT plan through Sign in with ChatGPT** is sent with `store: false`. OpenAI's documentation for this says plan usage gives the app no access to the user's ChatGPT conversations, but says nothing more on retention or training. Consumer plans also carry different terms from business plans and API access.
- **A model the firm runs itself** keeps everything on the firm's own machines.
