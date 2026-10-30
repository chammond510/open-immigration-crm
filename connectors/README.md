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
2. Implement `test_connection()`, plus:
   - `sync()` for the capabilities listed in `sync_capabilities` (the worker runs it every `sync_interval`);
   - `perform()` for outbound actions;
   - for providers that push events, a `webhooks` spec plus `verify_webhook()` (constant-time signature check; never accept unsigned requests), `parse_webhook()` (keep only what processing needs), and `handle_webhook()`.

   Raise `ConnectorError` for provider failures. Its message is stored and shown to staff, so never include client data in it. Synced and webhook records reach features through the `crm.connect.signals.records_received` signal; calls, texts, and emails are stored with `crm.comms.services.record_communication`, which keeps one copy per provider ID, and case status connectors record readings with `crm.notices.services.record_status`.
3. Describe in `data_flows` exactly what the connector sends and receives, in plain language. Raise its `version` whenever that changes, so firms review it again.
4. Never call `perform()` from feature code. Use `crm.connect.policy.send()`, which applies the outbound policy (kill switch, preview mode, data-flow acknowledgment, personal-connection rules, admin-only setting, texting opt-outs, and hourly limit) and records every attempt.
5. Add a test case that mixes in `crm.connect.testing.ConnectorContract`, with `sync_examples`, `outbound_examples`, and `make_webhook()` as needed. The contract tests block network access, so use recorded fixtures with fictional `.test` data.
6. Remove the provider's entry from `catalog.toml` once its connector exists.

Ready connectors need a named maintainer. `connectors/example_oauth/` shows an OAuth connector; its sign-in flow runs through the firm's own app registration, entered by a superuser on the connector's page.
