# Building a connector outside this repository

A connector can live in this repository, as the ones in `connectors/` do, or in a package of its own that a firm installs beside the CRM. This guide is for the second kind. [connectors/README.md](../connectors/README.md) explains what each capability expects of a connector, wherever it lives.

## Start from a skeleton

From a checkout of the CRM:

```bash
python manage.py startconnector fictional_shipper --name "Fictional Shipper" --capability shipping --directory ~/code
```

It writes `open-immigration-crm-fictional-shipper/`:

- `pyproject.toml`, which registers the connector through the `open_immigration_crm.connectors` entry point;
- `oicrm_fictional_shipper/__init__.py`, a connector whose manifest is complete;
- `oicrm_fictional_shipper/test_fictional_shipper.py`, the shared contract tests against a fake provider;
- `README.md`, the steps below.

The skeleton loads and its tests pass before you change anything. Replace each "Replace with" text, then build out the capability. The skeleton is not offered for automation, which is built in, or for staff sign-in, whose contract tests need a fake provider that proves a sign-in; start a sign-in connector from `connectors/oidc/`.

## Develop and test it

```bash
pip install -e ~/code/open-immigration-crm-fictional-shipper
python manage.py test oicrm_fictional_shipper
```

Run the tests from the CRM's checkout, with the package installed in the CRM's environment. Every connector test case mixes in `crm.connect.testing.ConnectorContract`, which checks the manifest, the credentials, syncs, outbound actions, webhooks, and sign-in for the capabilities the connector declares. The contract tests block the network: answer from a fake transport (`crm.connect.http.use_transport` with `httpx.MockTransport`), built from the provider's documentation, with fictional data only. Never commit anything recorded from a real account.

When the CRM starts, the registry loads the connector through its entry point. If it cannot, `python manage.py check` says why (`connect.W001`), and the CRM runs without it.

## What a connector may use

A connector package may import:

- `crm.connect.base`: `Connector`, the auth and field types, `DataFlows`, `WebhookSpec`, `WebhookEnvelope`, `StaffIdentity`, `MessageTemplate`, `TemplateField`, `SyncBatch`, `ConnectorError`, `Capability`, `Maturity`, `ConnectionScope`, and `API_VERSION`;
- `crm.connect.records`: the typed records each capability reports;
- `crm.connect.http`: `ProviderClient`, which adds timeouts, retries, size limits, and HTTPS checks, and `use_transport` for tests;
- `crm.connect.testing`: `ConnectorContract`;
- `crm.connect.registry`: `register`.

Everything else in `crm/` is the CRM's own and may change in any release. A connector never reads or writes the CRM's database: it returns records, and core decides what to keep.

## The interface version

`crm.connect.base.API_VERSION` is the connector interface's version, now `(1, 0)`. The first number changes when a change would break connectors written for the old interface; the second when the interface only grows. A connector declares the version it was written for in `api_version`, as the skeleton does. An installation refuses a connector written for a different first number, or a later second number, with that reason, rather than failing later on a missing feature.

## Before firms use it

- **Say exactly what moves.** `data_flows` is what a superuser reviews before the connection is used. Raise its `version` whenever it changes, so firms review it again.
- **Send through the policy.** Core calls `perform()` only through the outbound policy, which holds a new connection in preview mode, enforces the firm's limits, and records each attempt. Keep client details out of errors and logs.
- **Fail closed.** Refuse a webhook that is unsigned or wrongly signed. If the provider cannot sign its webhooks, trust a delivery only as a hint and fetch the facts from the provider.
- **Maturity.** A new connector is Beta. It can be marked Ready once it has passed a live check against a real account, run by hand on a non-production installation, and has a named maintainer in `maintainers`.
- **License.** The CRM is licensed under the AGPL-3.0, and a connector package runs inside it. Choose a license compatible with that; this guide is not legal advice.

Name a package `open-immigration-crm-<key>` and its module `oicrm_<key>`, as the skeleton does, so firms can tell what it is.
