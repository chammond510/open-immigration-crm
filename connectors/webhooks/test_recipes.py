"""Check the automation recipes in docs/recipes against the CRM.

The recipes' own code runs in Node, against deliveries this connector signs, and
what it builds is sent to the real endpoints. Make's formulas are not run.
"""

import base64
import json
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from django.test import SimpleTestCase
from django.urls import resolve, reverse
from django.utils import timezone

from crm.api.tests.helpers import ApiTestCase
from crm.connect.models import Connection, WebhookEvent
from crm.connect.tasks import process_webhook_event
from crm.events.catalog import EVENT_TYPES
from crm.events.services import build_event
from crm.models import Contact, WorkItem
from crm.notices.models import CaseStatusCheck
from crm.tests.factories import make_contact, make_matter

from . import WebhooksConnector, new_secret, sign
from .test_webhooks import FakeReceiver

RECIPES = Path(__file__).resolve().parents[2] / "docs" / "recipes"
PLACEHOLDER = "PASTE_THE_SIGNING_SECRET"
NODE = shutil.which("node")

# Runs one recipe step in Node the way its tool would: an n8n Code node, a Code by
# Zapier step, or n8n {{ expressions }} in a node's parameter.
HARNESS = r"""
const job = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const AsyncFunction = (async () => {}).constructor;
async function run() {
  if (job.kind === 'n8n') {
    const items = job.items.map((json) => ({ json }));
    const $input = { first: () => items[0], all: () => items };
    const sandbox = { helpers: {
      getBinaryDataBuffer: async (index, name) => Buffer.from(job.binary[index][name], 'base64'),
    } };
    return new AsyncFunction('$input', 'require', job.code).call(sandbox, $input, require);
  }
  if (job.kind === 'zapier') {
    return new Function('inputData', 'require', `let output;\n${job.code}\nreturn output;`)(
      job.inputData, require);
  }
  const $ = (name) => ({ first: () => ({ json: job.nodes[name] }) });
  const evaluate = (expr) => new Function('$', '$json', `return (${expr});`)($, job.json);
  const whole = job.text.match(/^=\{\{([\s\S]+)\}\}$/);
  if (whole) return evaluate(whole[1]);
  return job.text.replace(/^=/, '').replace(/\{\{([\s\S]+?)\}\}/g, (_, expr) => evaluate(expr));
}
run().then(
  (result) => process.stdout.write(JSON.stringify(result === undefined ? null : result)),
  (error) => { process.stderr.write(String(error)); process.exit(1); },
);
"""


def run_js(job):
    done = subprocess.run(
        [NODE, "-e", HARNESS],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def render(text, *, nodes=None, item=None):
    """An n8n parameter's value, with its {{ expressions }} worked out."""
    return run_js({"kind": "template", "text": text, "nodes": nodes or {}, "json": item or {}})


def workflow(name):
    data = json.loads((RECIPES / "n8n" / name).read_text())
    return data, {node["name"]: node for node in data["nodes"]}


def code_blocks(page, language):
    return re.findall(rf"```{language}\n(.*?)```", (RECIPES / page).read_text(), re.S)


def recipe_files():
    return sorted(path for path in RECIPES.rglob("*") if path.suffix in (".md", ".json"))


def slug(heading):
    return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")


class RecipeReferenceTests(SimpleTestCase):
    def test_event_types_named_exist(self):
        objects = {name.split(".")[0] for name in EVENT_TYPES} | {"webhook"}
        named = set()
        for path in recipe_files():
            for prefix, suffix in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", path.read_text()):
                if prefix in objects:
                    named.add(f"{prefix}.{suffix}")
        self.assertIn("case_status.changed", named)
        self.assertEqual(named - set(EVENT_TYPES), {"webhook.test"})

    def test_api_paths_and_addresses_resolve(self):
        paths = set()
        for path in recipe_files():
            paths.update(re.findall(r"/api/v1/[\w/<>-]*", path.read_text()))
        self.assertIn("/api/v1/work-items/", paths)

        def endpoint(path):
            # IDs are whole numbers or UUIDs; the API answers anything else as not found.
            for value in ("1", "00000000-0000-0000-0000-000000000001"):
                name = resolve(re.sub(r"<[^>]+>", value, path)).url_name
                if name:
                    return name
            return None

        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(endpoint(path), "an API endpoint")
        self.assertIsNone(endpoint("/api/v1/no-such-thing/"))
        _, nodes = workflow("website-form-lead.json")
        address = urlsplit(nodes["Send to the CRM"]["parameters"]["url"]).path
        self.assertEqual(resolve(address).url_name, "connector_webhook")

    def test_links_between_pages_work(self):
        for page in RECIPES.glob("*.md"):
            for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text()):
                if target.startswith("https://"):
                    continue
                with self.subTest(page=page.name, link=target):
                    file, _, anchor = target.partition("#")
                    linked = (page.parent / file).resolve() if file else page
                    self.assertTrue(linked.exists())
                    if anchor:
                        headings = re.findall(r"^#+ (.+)$", linked.read_text(), re.M)
                        self.assertIn(anchor, [slug(h) for h in headings])

    def test_workflows_are_complete_and_carry_no_secrets(self):
        versions = {
            "n8n-nodes-base.webhook": 2,
            "n8n-nodes-base.code": 2,
            "n8n-nodes-base.if": 2.2,
            "n8n-nodes-base.respondToWebhook": 1.1,
            "n8n-nodes-base.httpRequest": 4.2,
        }
        for path in sorted((RECIPES / "n8n").glob("*.json")):
            with self.subTest(workflow=path.name):
                text = path.read_text()
                data, nodes = workflow(path.name)
                self.assertEqual(len(nodes), len(data["nodes"]))
                for node in data["nodes"]:
                    self.assertEqual(node["typeVersion"], versions[node["type"]])
                for source, outputs in data["connections"].items():
                    self.assertIn(source, nodes)
                    for branch in outputs["main"]:
                        for target in branch:
                            self.assertIn(target["node"], nodes)
                self.assertEqual(data["settings"]["executionOrder"], "v1")
                self.assertNotRegex(text, r"whsec_[A-Za-z0-9+/]{8}|oic_")
                for node in nodes.values():
                    if node["type"] == "n8n-nodes-base.code":
                        code = node["parameters"]["jsCode"]
                        self.assertIn(f"const secret = '{PLACEHOLDER}';", code)
                    if node["type"] == "n8n-nodes-base.httpRequest":
                        self.assertTrue(node["parameters"]["url"].startswith("https://"))


class RecipeCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.secret = new_secret()
        self.matter = make_matter(make_contact(self.user), self.user)
        self.receiver = FakeReceiver()
        patcher = mock.patch("connectors.webhooks.opener", self.receiver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def delivery(self, event_type="case_status.changed"):
        """The body and headers of one event, exactly as the CRM sends it."""
        connection = Connection.objects.create(
            connector_key="webhooks",
            label="Recipe receiver",
            enabled_capabilities=["automation"],
            credentials={"url": "https://hooks.example.test/recipe", "signing_secret": self.secret},
        )
        check = CaseStatusCheck.objects.create(
            matter=self.matter, receipt_number="IOE0900000001", status_title="Case Was Approved"
        )
        event = build_event(event_type, check, connection, occurred_at=timezone.now())
        WebhooksConnector(connection).post(f"msg_{check.pk}", event)
        request = self.receiver.requests[-1]
        return request.data, {name.lower(): value for name, value in request.header_items()}

    def resigned(self, body, headers, *, at=None, secret=None):
        stamp = str(int(at or time.time()))
        return {
            **headers,
            "webhook-timestamp": stamp,
            "webhook-signature": sign(secret or self.secret, headers["webhook-id"], stamp, body),
        }

    def post_task(self, body):
        _, token = self.issue("work:write")
        answer = self.api("POST", "/api/v1/work-items/", json.loads(body), token=token)
        self.assertEqual(answer.status_code, 201, answer.content)
        item = WorkItem.objects.get(pk=answer.json()["id"])
        self.assertEqual((item.matter, item.kind, item.priority), (self.matter, "task", "high"))
        return item

    def website_form(self):
        """A Website form connection's address, set up as a superuser would."""
        self.client.force_login(self.user)
        answer = self.client.post(
            reverse("connector_detail", args=["website_form"]),
            {
                "label": "Website form",
                "capabilities": ["lead_capture"],
                "credential_signing_secret": self.secret,
                "setting_lead_source_from": "source",
                "acknowledge": "on",
            },
        )
        self.assertEqual(answer.status_code, 302, answer.context and answer.context["form"].errors)
        connection = Connection.objects.get(connector_key="website_form")
        response = self.client.post(reverse("connection_webhooks", args=[connection.pk]))
        return response.context["webhook_url"]

    def submit(self, url, body, headers):
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(url, body, content_type="application/json", headers=headers)
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer


@unittest.skipUnless(NODE, "Node.js runs the recipes' code")
class N8nRecipeTests(RecipeCase):
    def check(self, body, headers, code):
        [result] = run_js({
            "kind": "n8n",
            "code": code,
            "items": [{"headers": headers, "body": json.loads(body)}],
            "binary": [{"data": base64.b64encode(body).decode()}],
        })
        return result["json"]

    def test_case_status_workflow_checks_the_event_and_adds_a_task(self):
        data, nodes = workflow("case-status-task.json")
        self.assertTrue(nodes["CRM event"]["parameters"]["options"]["rawBody"])
        self.assertEqual(nodes["CRM event"]["parameters"]["responseMode"], "responseNode")
        self.assertEqual(data["connections"]["Signed?"]["main"][1][0]["node"], "Refuse")
        jscode = nodes["Check signature"]["parameters"]["jsCode"]
        with self.assertRaisesMessage(AssertionError, "Paste the signing secret"):
            self.check(*self.delivery(), jscode)
        code = jscode.replace(PLACEHOLDER, self.secret)
        body, headers = self.delivery()
        checked = self.check(body, headers, code)
        self.assertEqual(
            (checked["valid"], checked["type"], checked["data"]["matter"]),
            (True, "case_status.changed", str(self.matter.pk)),
        )

        def condition(name, item):
            [rule] = nodes[name]["parameters"]["conditions"]["conditions"]
            return render(rule["leftValue"], nodes={"Check signature": checked}, item=item), rule

        self.assertIs(condition("Signed?", checked)[0], True)
        value, rule = condition("Status change?", {})
        self.assertEqual(value, rule["rightValue"])
        parameters = nodes["Add a task"]["parameters"]
        self.assertEqual(urlsplit(parameters["url"]).path, "/api/v1/work-items/")
        self.assertEqual(parameters["genericAuthType"], "httpBearerAuth")
        self.post_task(render(parameters["jsonBody"], nodes={"Check signature": checked}))

        refused = {
            "changed body": (body[:-1] + b" }", headers),
            "old": (body, self.resigned(body, headers, at=time.time() - 600)),
            "other secret": (body, self.resigned(body, headers, secret=new_secret())),
            "unsigned": (body, {**headers, "webhook-signature": ""}),
        }
        for case, (sent, sent_headers) in refused.items():
            with self.subTest(case=case):
                outcome = self.check(sent, sent_headers, code)
                self.assertEqual(outcome, {"valid": False})
                self.assertIs(condition("Signed?", outcome)[0], False)

    def test_website_form_workflow_sends_a_signed_lead(self):
        url = self.website_form()
        _, nodes = workflow("website-form-lead.json")
        self.assertEqual(nodes["Website form"]["parameters"]["authentication"], "headerAuth")
        code = nodes["Sign for the CRM"]["parameters"]["jsCode"].replace(PLACEHOLDER, self.secret)
        form = {
            "name": "Lena Fictional",
            "email": "lena@example.test",
            "phone": "202-555-0144",
            "message": "Can you help with a green card?",
            "utm_source": "google-ads",
        }
        [result] = run_js({"kind": "n8n", "code": code, "items": [{"body": form}], "binary": []})
        signed = result["json"]
        parameters = nodes["Send to the CRM"]["parameters"]
        self.assertEqual((parameters["contentType"], parameters["rawContentType"]),
                         ("raw", "application/json"))
        headers = {
            header["name"]: render(header["value"], item=signed)
            for header in parameters["headerParameters"]["parameters"]
        }
        body = render(parameters["body"], item=signed)
        self.assertEqual(json.loads(body)["form"], "Website form")
        self.assertEqual(self.submit(url, body, headers).status_code, 200)
        contact = Contact.objects.get(email="lena@example.test")
        self.assertEqual((contact.full_name, contact.stage), ("Lena Fictional", "inquiry"))
        # The same request again, or one signed with another secret, files nothing new.
        self.assertEqual(self.submit(url, body, headers).status_code, 200)
        forged = self.resigned(body.encode(), headers, secret=new_secret())
        self.assertEqual(self.submit(url, body, forged).status_code, 401)
        self.assertEqual(Contact.objects.filter(email="lena@example.test").count(), 1)


@unittest.skipUnless(NODE, "Node.js runs the recipes' code")
class ZapierRecipeTests(RecipeCase):
    def test_zap_checks_the_event_and_adds_a_task(self):
        verify, _ = code_blocks("zapier.md", "js")
        [task] = code_blocks("zapier.md", "json")
        body, headers = self.delivery()

        def zap(sent_headers, secret=self.secret):
            return run_js({"kind": "zapier", "code": verify, "inputData": {
                "body": body.decode(),
                "id": sent_headers["webhook-id"],
                "timestamp": sent_headers["webhook-timestamp"],
                "signature": sent_headers["webhook-signature"],
                "secret": secret,
            }})

        output = zap(headers)
        self.assertEqual(
            output,
            {"valid": "true", "type": "case_status.changed", "matter": str(self.matter.pk)},
        )
        self.post_task(task.replace("MATTER", output["matter"]))
        self.assertEqual(zap(headers, new_secret())["valid"], "false")
        stale = self.resigned(body, headers, at=time.time() - 600)
        self.assertEqual(zap(stale), {"valid": "false", "type": "", "matter": ""})

    def test_zap_sends_a_signed_lead(self):
        url = self.website_form()
        _, sign_lead = code_blocks("zapier.md", "js")
        output = run_js({"kind": "zapier", "code": sign_lead, "inputData": {
            "name": "Omar Fictional",
            "email": "omar@example.test",
            "phone": "",
            "message": "Do I qualify for a waiver?",
            "secret": self.secret,
        }})
        self.assertNotIn("phone", json.loads(output["body"]))
        headers = {
            "webhook-id": output["id"],
            "webhook-timestamp": output["timestamp"],
            "webhook-signature": output["signature"],
        }
        self.assertEqual(self.submit(url, output["body"], headers).status_code, 200)
        self.assertTrue(Contact.objects.filter(email="omar@example.test", stage="inquiry").exists())


class MakeRecipeTests(RecipeCase):
    def test_scenario_task_body_is_accepted(self):
        [task] = code_blocks("make.md", "json")
        self.post_task(task.replace("MATTER", str(self.matter.pk)))
