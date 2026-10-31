# Zapier recipes

Steps for two Zaps. See [the recipes overview](README.md) for what they do and where data goes. Webhooks by Zapier and filters need a paid Zapier plan.

Zapier cannot keep a secret out of a Zap. The signing secret goes into a code step's input data, where anyone who can edit the Zap can read it. Zap history also keeps each step's data for up to two months.

## Task when a USCIS status changes

1. **Trigger: Webhooks by Zapier → Catch Raw Hook.** Copy the webhook URL.
   - Use Catch Raw Hook, not Catch Hook. Catch Hook parses the body, and the signature can only be checked against the raw body.
2. **In the CRM**, open **Connections → Automation → Webhooks**, paste the URL, and copy the signing secret. Then connect.
   - The CRM sends a `webhook.test` event. Back in Zapier, test the trigger to load it.
3. **Action: Code by Zapier → Run JavaScript.** Under **Input Data**, add:
   - `body`: the trigger's raw body.
   - `id`, `timestamp`, `signature`: the trigger's `webhook-id`, `webhook-timestamp`, and `webhook-signature` headers. Zapier lists them among the trigger's headers.
   - `secret`: the signing secret, typed in.

   Then use this code:

   ```js
   const crypto = require('crypto');

   const key = Buffer.from(inputData.secret.replace(/^whsec_/, ''), 'base64');
   const hmac = crypto.createHmac('sha256', key);
   hmac.update(`${inputData.id}.${inputData.timestamp}.${inputData.body}`);
   const expected = Buffer.from(`v1,${hmac.digest('base64')}`);
   const signed = (inputData.signature || '').split(' ').some((candidate) => {
     const given = Buffer.from(candidate);
     return given.length === expected.length && crypto.timingSafeEqual(given, expected);
   });
   const fresh = Math.abs(Date.now() / 1000 - Number(inputData.timestamp)) < 300;
   const event = signed && fresh ? JSON.parse(inputData.body) : { data: {} };
   output = {
     valid: String(signed && fresh),
     type: event.type || '',
     matter: event.data.matter || '',
   };
   ```

   Test the step with the CRM's test event: `valid` should be `true`.
   - If it is `false`, check the secret and the mapped fields.
   - The test passes only within five minutes of the event. For a fresh one, test the connection again in the CRM.
4. **Filter by Zapier.** Continue only if:
   - `valid` (Text) Exactly matches `true`, **and**
   - `type` (Text) Exactly matches `case_status.changed`.
5. **Action: Webhooks by Zapier → Custom Request.**
   - **Method:** `POST`.
   - **URL:** the CRM's address followed by `/api/v1/work-items/`.
   - **Data:**

     ```json
     {"kind": "task", "priority": "high", "matter": "MATTER", "title": "Tell the client about their new USCIS case status"}
     ```

     Replace `MATTER` with the code step's `matter`.
   - **Headers:**
     - `Content-Type`: `application/json`
     - `Authorization`: `Bearer` followed by a space and an [API token](../API.md#tokens) that has only the `work:write` scope.
6. **Publish the Zap.** In the CRM, choose the `case_status.changed` event with IDs and links only, then turn preview mode off.

## Website form leads

1. **Trigger:** the form app's own Zapier trigger, such as a new form entry from the firm's WordPress form plugin.
2. **In the CRM**, someone who manages connections and AI opens **Connections → Website form**, copies the signing secret, and connects. On the connection's page, turn on webhooks and copy the address.
3. **Action: Code by Zapier → Run JavaScript.** Under **Input Data**:
   - Map `name`, `email`, `phone`, `message`, and, if the form tracks it, `source` from the form's fields.
   - Add `secret`, the signing secret, typed in.

   Then use this code:

   ```js
   const crypto = require('crypto');

   const fields = {
     name: inputData.name,
     email: inputData.email,
     phone: inputData.phone,
     message: inputData.message,
     form: inputData.form || 'Website form',
     source: inputData.source,
   };
   // Leave out the fields the form left empty.
   const submission = Object.fromEntries(Object.entries(fields).filter(([, value]) => value));
   const body = JSON.stringify(submission);
   const id = `msg_${crypto.randomUUID()}`;
   const timestamp = String(Math.floor(Date.now() / 1000));
   const key = Buffer.from(inputData.secret.replace(/^whsec_/, ''), 'base64');
   const hmac = crypto.createHmac('sha256', key).update(`${id}.${timestamp}.${body}`);
   output = { body, id, timestamp, signature: `v1,${hmac.digest('base64')}` };
   ```

4. **Action: Webhooks by Zapier → Custom Request.**
   - **Method:** `POST`.
   - **URL:** the connection's address.
   - **Data:** the code step's `body`, and nothing else. Custom Request sends it exactly as entered. Do not use data pass-through.
   - **Headers:**
     - `Content-Type`: `application/json`
     - `webhook-id`, `webhook-timestamp`, `webhook-signature`: the code step's `id`, `timestamp`, and `signature`.
5. **Test and publish.** The CRM answers `200` when it accepted the submission and `401` when the signature or time is wrong. A test fails if more than five minutes pass between the code step and the request, so test the two steps together.
