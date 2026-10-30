# Roadmap and product boundary

## Priorities

1. Accessibility and keyboard/screen-reader quality
2. Safer operations: backup verification, upgrade checks, and security documentation
3. Data portability and deliberate deletion/retention tools
4. Performance and usability for small-firm workloads
5. Optional connectors to the services firms already use

## Connector roadmap

The [connector plan](CONNECTOR_PLAN.md) has the full design. Each phase must pass the release gate before the next begins.

0. **Boundary** (done). Documentation and check scripts describe a provider-neutral core plus optional connectors.
1. **Foundation** (done). Encrypted credential storage, built-in two-factor sign-in, a database-backed worker, the connector registry, the Connections page, webhooks, and the outbound policy, proven with a fictional example connector.
2. **Manual-first features.** A communications log, billing, receipt tracking, firm-authored workflow templates, custom fields, a REST API, and signed outbound webhooks, all usable with nothing connected.
3. **First connectors and AI access.** Phone, mailbox, calendar, scheduling, payments, AI models, legal research, and case status, plus an MCP server so staff can use the CRM from their own AI assistant.
4. **Second providers.** A second provider for each capability, so the interfaces stay provider-neutral.
5. **Ecosystem.** A connector SDK, third-party connector packages, migration importers, and more providers.
6. **Hosted connection service (optional).** One-click sign-in for providers that otherwise need each firm to register its own OAuth app.

AI features stay opt-in and off by default at every phase.

## Non-goals

- Autonomous legal work: the software does not file, sign, or decide anything on a matter by itself, and AI output is always a draft for attorney review
- Shipped legal content: forms, filing strategies, case-type checklists, intake-question templates, or workflow templates
- Multi-tenant SaaS administration
- Background-queue infrastructure such as Redis or a message broker; jobs use the database
- Telemetry or usage reporting from installations

Core should remain understandable by a small firm and a small maintainer team. A connector that cannot meet that bar belongs in a separate package.
