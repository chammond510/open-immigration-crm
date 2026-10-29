# Contributing

Thank you for helping keep Open Immigration CRM small, legible, and safe.

## Product boundary

Contributions to core should strengthen contacts, matters, work, documents, checklists, intake, portability, accessibility, security, or operations. Core stays provider-neutral: code that talks to an outside service belongs in a connector under `connectors/`, never in `crm/`. Read the [connector plan](docs/CONNECTOR_PLAN.md) before proposing one. Open a discussion first for a new capability (a new kind of service, rather than a new provider for an existing capability).

Shipped legal content (forms, filing strategies, case-type checklists, intake questions, or workflow templates) stays out of the project.

Never contribute real client data, privileged material, credentials, database dumps, logs, screenshots with identifying information, firm branding, or legal templates derived from client work. All examples and tests must use obviously fictional `.test` addresses and reserved/example identifiers.

## Development

1. Create a focused branch from current `main`.
2. Install `requirements-dev.lock` in a virtual environment.
3. Add tests for behavioral changes.
4. Run `./scripts/check_release.sh`.
5. Explain security and migration impact in the pull request.

## Contributing a connector

Connector contributions open once the foundation phase of the connector plan has landed. A connector pull request needs:

1. A manifest: name, capabilities, authentication type, maturity, and a plain-language list of the data it sends and receives.
2. Passing contract tests for each capability it implements, run against recorded fixtures. CI never calls a live service.
3. Fixtures that use only fictional `.test` data and pass `./scripts/check_privacy.sh`.
4. A named maintainer. Connectors without one are labeled Beta, not Ready.
5. Plain HTTPS calls by default. A provider SDK may be added only as an optional extra, with its license recorded in `THIRD_PARTY_NOTICES.md`.
6. An OSI-approved license compatible with AGPL-3.0.

By contributing, you agree that your contribution is licensed under AGPL-3.0-only.
