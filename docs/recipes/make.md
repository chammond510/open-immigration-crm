# Make recipes

Steps for two Make scenarios. See [the recipes overview](README.md) for what they do and where data goes.

Before running either scenario with client data, open the scenario's settings (the gear icon) and turn on **Keep data confidential**. Make then keeps no copy of what passes through a run.

Make cannot keep the signing secret out of a scenario: it goes into a formula, where anyone who can edit the scenario can read it. The API token goes in an encrypted key instead. Make cannot compare signatures in constant time either. The five-minute limit on timestamps narrows what a timing attack could do, but the n8n and Zapier recipes compare more safely.

In the formulas below, a name in angle brackets is an item you map from the mapping panel:
- `<body>` is the webhook's raw body.
- `<webhook-id>`, `<webhook-timestamp>`, and `<webhook-signature>` are the headers with those names.
- `SECRET` is the part of the signing secret after `whsec_`, typed in.

## Task when a USCIS status changes

1. **Trigger: Webhooks → Custom webhook.** Add a webhook, then show its advanced settings and set:
   - **Get request headers** to **Yes**.
   - **JSON pass-through** to **Yes**, so the body arrives as the exact text that was signed.

   Copy the address.
2. **In the CRM**, open **Connections → Automation → Webhooks**, paste the address, and copy the signing secret. Then connect.
   - The CRM sends a `webhook.test` event, and Make learns the request's fields from it.
   - Make lists the headers as name and value pairs. One way to map a header's value is `first(map(<headers>; "value"; "name"; "webhook-id"))`.
3. **Flow Control → If-else.** Add one condition with three rules, all of which must hold:
   - **Boolean operators: Equal to** `true`, for:

     ```
     contains(split(<webhook-signature>; " "); "v1," + sha256(<webhook-id> + "." + <webhook-timestamp> + "." + <body>; "base64"; "SECRET"; "base64"))
     ```

   - **Date operators: Later than**, with `parseDate(<webhook-timestamp>; "X")` on the left and `addMinutes(now; -5)` on the right.
   - **Date operators: Earlier than**, with `parseDate(<webhook-timestamp>; "X")` on the left and `addMinutes(now; 5)` on the right.
4. **On the condition's route:**
   1. **Webhooks → Webhook response**, with status `200`.
   2. **JSON → Parse JSON** on `<body>`.
   3. **A filter** that lets through only `type` **Equal to** `case_status.changed`.
   4. **HTTP → Make a request**, set as follows:
      - **URL:** the CRM's address followed by `/api/v1/work-items/`.
      - **Method:** `POST`.
      - **Authentication type:** **API key**. Create a key in which **Key** is `Bearer` followed by a space and an [API token](../API.md#tokens) that has only the `work:write` scope. **API key placement** is the header, and **API key parameter name** is `Authorization`.
      - **Body content type:** `application/json`. **Body input method:** JSON string. **Body:**

        ```json
        {"kind": "task", "priority": "high", "matter": "MATTER", "title": "Tell the client about their new USCIS case status"}
        ```

        Map the parsed `data.matter` in place of `MATTER`.
5. **On the Else route: Webhooks → Webhook response**, with status `401`.
6. **Run the scenario once** and test the CRM connection. The test event should take the condition's route.
   - **If it takes the Else route,** check the secret, then check whether the test is more than five minutes old.
   - **If both are right,** Make changed the body before the check, and this scenario cannot check signatures. Use the n8n recipe instead.
7. **Choose the event.** In the CRM, choose `case_status.changed` with IDs and links only, then turn preview mode off.

Don't route a bad signature into an error. Make turns off a scenario that starts with a webhook at its first error, so a single forged request would stop it.

## Website form leads

1. **Trigger:** the form app's own Make module, or a custom webhook that the website posts to.
2. **In the CRM**, someone who manages connections and AI opens **Connections → Website form**, copies the signing secret, and connects. On the connection's page, turn on webhooks and copy the address.
3. **JSON → Create JSON.** Build the submission from the form's fields: `name`, `email`, `phone`, `message`, `form`, and, if the form tracks it, `source`. Its output is the JSON text to sign. Call it `<json>` below.
4. **Tools → Set multiple variables:**
   - `id`: `msg_` followed by Make's `uuid`.
   - `timestamp`: `formatDate(now; "X")`.
   - `signature`:

     ```
     "v1," + sha256(<id> + "." + <timestamp> + "." + <json>; "base64"; "SECRET"; "base64")
     ```

5. **HTTP → Make a request:**
   - **URL:** the connection's address. **Method:** `POST`.
   - **Headers:** `webhook-id`, `webhook-timestamp`, and `webhook-signature`, set to the three variables.
   - **Body content type:** `application/json`. **Body input method:** JSON string. **Body:** `<json>`, and nothing else.
6. **Run it once.** The CRM answers `200` when it accepted the submission and `401` when the signature or time is wrong. A `401` with the right secret means Make sent different bytes from the ones it signed.
