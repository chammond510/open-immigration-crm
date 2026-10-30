# AI assistants (MCP server)

The CRM includes a [Model Context Protocol](https://modelcontextprotocol.io/) server, so staff can add it to Claude or ChatGPT and work with contacts, matters, deadlines, and notices from their own assistant, on their own plan. The CRM never holds Claude or OpenAI credentials: the assistant does the AI work and calls the CRM's tools.

The server is **off** on a new installation. A superuser turns it on under **AI assistants** (in the sidebar's account links).

## How it works

- **Signing in.** Each staff member connects their own assistant.
  - The assistant sends them to a consent page in the CRM. They sign in as usual, including two-factor sign-in, and approve.
  - The assistant gets a token that acts as that person, with their permissions, for up to an hour at a time. It renews for up to 30 days, and the person can disconnect it at any time.
- **Reading.** Read tools are available to every connected assistant:
  - search contacts and matters;
  - contact and matter summaries;
  - upcoming work;
  - USCIS notices;
  - communications;
  - your time;
  - the operating summary;
  - invoices, for staff with billing access.
- **Writing.** Write tools are off until a superuser allows them: create a task, add a note, log a communication, record a status check, and mark work done.
  - Each write tool is marked as not read-only, so Claude and ChatGPT ask the person before running it.
  - Nothing can be deleted through MCP.
  - Changes are audited as coming through MCP, with the assistant's name.
- **The log.** Every tool call is logged with who, which assistant, which tool, and whether it worked. What was asked and returned is not stored.

## Connect Claude

Custom connectors work on every Claude plan. Free is limited to one.

1. In the CRM, open **AI assistants** and copy the server URL. It ends in `/mcp`, with no trailing slash.
2. In Claude, open **Settings → Connectors → Add custom connector** and paste the URL. On Team and Enterprise, an owner adds the connector once and each member connects.
3. Choose **Connect**. A CRM page opens. Sign in if asked, check that the page names the assistant you just started and says you will return to `claude.ai`, then choose **Allow**.

Claude registers itself automatically. Claude Code is not supported yet: its sign-in returns to a plain-http address on your own computer, which the CRM does not accept.

## Connect ChatGPT

OpenAI documents custom MCP connectors for ChatGPT Business, Enterprise, and Edu on the web. Other plans were unconfirmed when this was written.

1. An admin creates a custom connector with the server URL and OAuth authentication.
2. Each person connects and approves on the CRM's consent page, as above.

## What must be reachable

Claude and ChatGPT call the CRM from their own servers, not from the staff member's browser.

These paths must be reachable over HTTPS from the internet, or at least from Claude's published range (`160.79.104.0/21`) and OpenAI's [published list](https://openai.com/chatgpt-connectors.json):
- `/mcp`
- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource/mcp`
- `/o/` (token and client registration)

The consent page, `/o/authorize/`, only needs to be reachable from staff browsers.

If the rest of the CRM stays behind a VPN, expose just these paths at the reverse proxy. ChatGPT's Secure MCP Tunnel can reach a private `/mcp`, but it does not carry the sign-in endpoints.

## Security

- **Sign-in standards.** Sign-in uses OAuth 2.1 through [django-oauth-toolkit](https://django-oauth-toolkit.readthedocs.io/):
  - PKCE with S256 only;
  - tokens bound to the `/mcp` resource (RFC 8707);
  - refresh tokens that rotate, where reusing an old one revokes the whole chain;
  - the RFC 9207 `iss` value on every authorization response.
- **Hashed tokens.** Only SHA-256 hashes of tokens are stored.
- **Client registration.** Assistants register through Client ID Metadata Documents, fetched only over HTTPS from public addresses, or through dynamic registration. Dynamic registration is capped at 30 new clients an hour, and both stop when the server is turned off.
- **Consent.** The consent page is shown every time. It names the assistant and the site it returns to, because anyone can register a client with any name.
- **Requests.** Requests from browsers on other sites are refused (the `Origin` check). Each person is limited to `MCP_HOURLY_LIMIT` tool calls an hour (default 1000).
- **Data terms.** Consumer AI plans carry different data-use terms from business plans and API access. Check your plan's terms before using client information.

## Protocol

`/mcp` is a stateless Streamable HTTP endpoint that answers every POST with a single JSON response.
- **Versions.** It serves MCP 2026-07-28 (`server/discover`, version headers, no sessions) and the earlier handshake used by 2025-11-25, 2025-06-18, and 2025-03-26 clients (`initialize`, then `tools/list` and `tools/call`).
- **Other methods.** GET and DELETE return 405.
- **Instructions.** The server's instructions tell the assistant to treat record text as data, not as instructions.
