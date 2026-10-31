# Automation recipes

Ready-made automations for n8n, Zapier, and Make that work with the CRM's [outbound webhooks](../WEBHOOKS.md), its [REST API](../API.md), and the [Website form connector](../../connectors/website_form/README.md).

| Recipe | What it does | n8n | Zapier | Make |
|---|---|---|---|---|
| **Task when a USCIS status changes** | When the CRM records a new USCIS case status, it adds a task on the matter to tell the client. | [Workflow to import](n8n/case-status-task.json) | [Steps](zapier.md#task-when-a-uscis-status-changes) | [Steps](make.md#task-when-a-uscis-status-changes) |
| **Website form leads** | Signs each submission from the firm's website form and sends it to the CRM, where it becomes a lead. | [Workflow to import](n8n/website-form-lead.json) | [Steps](zapier.md#website-form-leads) | [Steps](make.md#website-form-leads) |

The guides: [n8n](n8n.md), [Zapier](zapier.md), [Make](make.md). Each recipe is a starting point; change the task, or act on another [event](../WEBHOOKS.md#events), the same way.

## What every recipe does

- **Checks the signature first.** Every CRM event is signed. A recipe checks the signature against the exact body it received, refuses an event more than five minutes old, and acts on nothing else. Without the check, anyone who learns the recipe's address could make it act. Zapier always answers the sender with success, so its recipe stops at a filter instead of refusing.
- **Answers, then acts.** The CRM waits 10 seconds for an answer and retries an unanswered delivery with the same `webhook-id`. The n8n and Make recipes answer as soon as the signature checks out, then do the work.
- **Sends IDs, not client details.** Set the Webhooks connection to send record IDs and links only. The recipe reads or writes anything else through the API, with a token whose scopes allow only that: `work:write` for the task recipe.
- **Signs what it sends.** The Website form connector refuses a submission that is unsigned, wrongly signed, or more than five minutes old. The recipe signs it with the connection's secret just before sending, and sends the signed bytes unchanged.

## Where client data and secrets go

- **Run history.** Each tool keeps a copy of what passes through a run.
  - **Self-hosted n8n** keeps it on the firm's own computers, and the website form workflow keeps no copy of runs that succeed.
  - **Zapier** keeps each step's data in Zap history for up to two months. Team members with access to the Zap can see it.
  - **Make** keeps module inputs and outputs in the scenario's history unless **Keep data confidential** is on in the scenario's settings. It also keeps a webhook's requests in the webhook's log for a few days.
- **Client details.** An event with IDs only carries none. A website form submission carries a name, contact details, and a message. Before sending client details through a hosted tool, make sure the firm's agreement with it covers them.
- **Secrets.**
  - **n8n.** An API token goes in a credential. The signing secret is pasted into a Code node, where exported copies of the workflow include it. On plans with Variables, keep it in a variable instead.
  - **Zapier and Make.** Anyone who can edit the Zap or scenario can see the signing secret.
  - **Replacing a secret.** If a secret may have leaked, disconnect the CRM connection and connect it again for a new one.

## Trying a recipe

- **New Webhooks connections start in preview mode.** Events are recorded on the connection's page but not sent. When the CRM connects, it sends a signed `webhook.test` event straight away, which the recipe should accept and otherwise ignore. Turn preview mode off when the recipe is ready.
- **The website form connection answers** `200` when it accepted a submission, `401` when the signature or time is wrong, and `400` when the body is not a usable submission.
- **If the test event fails the check,** the secret is wrong or the tool changed the body before the check. The CRM's events are plain ASCII JSON, so a tool that passes the body on unchanged always checks it correctly.

`connectors/webhooks/test_recipes.py` checks these recipes against the CRM:
- It runs the n8n Code nodes and the Zapier code steps against events the CRM signed.
- It sends the requests they build to the real endpoints.
- It checks that every event type and API path these pages name exists.

The tests do not run Make's formulas, and none of the recipes has been run in the hosted tools by these tests.
