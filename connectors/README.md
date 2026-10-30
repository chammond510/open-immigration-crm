# Connectors

Each subpackage here is one optional provider adapter. Core code never imports these packages; `crm/connect/registry.py` loads them at startup, and installed third-party packages can add more through the `open_immigration_crm.connectors` entry point. The design is in [docs/CONNECTOR_PLAN.md](../docs/CONNECTOR_PLAN.md).

## Layout

```text
connectors/
  catalog.toml          wanted providers shown in the catalog before a connector exists
  example/              a fictional connector that tests the framework (hidden from the catalog)
    __init__.py         the connector class, registered with @register
    test_example.py     the shared contract tests plus connector-specific tests
```

## Writing a connector

1. Subclass `crm.connect.base.Connector` and declare the manifest as class attributes: `key`, `name`, `description`, `maturity`, `capabilities`, `auth`, `connection_scope`, `data_flows`, `maintainers`, and `outbound_actions`. Register the class with `@register` from `crm.connect.registry`. A manifest that is incomplete is rejected at startup.
2. Implement `test_connection()`, plus `sync()` for capabilities that pull records and `perform()` for outbound actions. Raise `ConnectorError` for provider failures. Its message is stored and shown to staff, so never include client data in it.
3. Describe in `data_flows` exactly what the connector sends and receives, in plain language. Raise its `version` whenever that changes, so firms review it again.
4. Never call `perform()` from feature code. Use `crm.connect.policy.send()`, which applies the outbound policy (kill switch, preview mode, data-flow acknowledgment, personal-connection rules, admin-only setting, and hourly limit) and records every attempt.
5. Add a test case that mixes in `crm.connect.testing.ConnectorContract`. The contract tests block network access, so use recorded fixtures with fictional `.test` data.
6. Remove the provider's entry from `catalog.toml` once its connector exists.

Ready connectors need a named maintainer. Connector contributions open fully once the rest of connector Phase 1 lands: the worker, webhooks, and the Connections page.
