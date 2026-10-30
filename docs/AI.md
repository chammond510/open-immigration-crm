# AI features

The CRM's own AI features use a model the firm connects. Everything is off on a new installation, and core sends nothing to an AI model until a superuser turns AI on and picks the features. This page covers connecting a model and the controls. The features themselves arrive separately.

This is separate from [AI assistants (MCP)](MCP.md), which let staff use the CRM from their own Claude or ChatGPT.

## Controls

- **Off by default.** In **AI features** (in the sidebar), a superuser turns on AI and each feature separately. While AI is off, no feature runs.
- **Who pays.** Each request runs through one connection, and the log records which.
  - A staff member's request runs through a connection they may use: the firm's, or their own.
  - Work no one is waiting on, such as preparing a memo before a consult, runs only through the firm-wide connection a superuser picks for background work, never a staff member's own.
- **The outbound policy.** Every request passes the same policy as texts and payments:
  - A new connection starts in preview mode, so nothing is sent until a superuser turns preview mode off on the connection's page.
  - A personal connection runs only its owner's own requests.
  - The hourly limit applies.
- **The log.** The AI features page lists recent requests: when, the feature, who asked (or "Background"), the connection, the model, the tokens in and out, and the outcome. Prompts and answers are not stored.
- **A check without client data.** A superuser can send a fixed test message through any AI connection to confirm it works.

## Connecting a model

| Connector | How it connects | Notes |
|---|---|---|
| **Anthropic** | A Claude Console API key (`sk-ant-api…`), and a workspace ID if the key isn't tied to one workspace | Claude.ai subscription sign-ins are refused. Anthropic does not permit apps to use or store Claude.ai credentials. Uses API version `2026-10-21`; the default model is `claude-opus-5-5`. |
| **OpenAI** | An API key and a model ID | Every request is sent with `store: false`, so OpenAI keeps no application state for later retrieval. |
| **OpenRouter** | A superuser signs in at OpenRouter, which hands back a key for the firm's account | Every request asks OpenRouter to route only to providers that don't store or train on data. A stricter setting allows only zero-data-retention providers. Disconnecting deletes the key here; delete it at openrouter.ai/keys too. |
| **OpenAI-compatible endpoint** | A server address and model name, and an API key if the server needs one | Covers models the firm runs itself, such as Ollama (`http://localhost:11434/v1`) or LM Studio. Plain HTTP is allowed only to this computer or the firm's own network. |

Then, in **AI features**, choose the connection for background work, if any. A superuser turns off preview mode on the connection when the firm is ready.

## Data terms

Check each provider's terms before sending client information. The firm remains responsible for that choice. As of October 2026, the providers' own documentation says:

- **Anthropic** does not train on commercial API content. It keeps flagged content longer, and offers zero data retention by arrangement.
- **OpenAI** does not use API data for training unless the firm opts in. It keeps abuse-monitoring logs for 30 days, and zero data retention needs its approval.
- **OpenRouter** stores no prompts or answers by default. The model providers it routes to have their own policies, which is why the CRM always asks it to deny data collection.
- **A model the firm runs itself** keeps everything on the firm's own machines.
