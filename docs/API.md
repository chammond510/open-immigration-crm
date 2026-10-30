# REST API

The CRM has a JSON API at `/api/v1/` for scripts and automation tools such as n8n, Zapier, and Make. It is built from plain Django views and uses the same validation as the web pages.

The API is **off** on a new installation. A superuser turns it on under **API access** (in the sidebar's account links). Nothing can be deleted through the API.

## Tokens

Every request carries a token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer $CRM_TOKEN" https://crm.example.test/api/v1/
```

Create a token under **API access → Create a token**.
- **Who it acts as.** A token acts as the staff member who created it. It has that person's access, and audit entries name them.
- **Scopes.** A token can reach only the records its scopes allow. Write access includes read access.
- **Shown once.** The token is displayed once. Only its SHA-256 hash is stored, so a lost token cannot be recovered. Revoke it and create another.
- **Expiry.** Every token expires after 30, 90, or 180 days, or one year.
- **Who can create tokens.** Only superusers, unless a superuser turns on **Let staff create their own tokens**. Turning that setting off again stops staff tokens from working.

A token stops working at once when any of these happens:
- it is revoked or expires;
- its user is deactivated or loses staff status;
- the API is turned off;
- the firm requires two-factor sign-in and the token's user has not set it up.

Tokens start with `oic_`, so secret scanners can find one that leaks. `scripts/check_privacy.sh` refuses to commit one.

| Scope | Records |
|---|---|
| `contacts:read`, `contacts:write` | Contacts |
| `matters:read`, `matters:write` | Matters and their parties |
| `work:read`, `work:write` | Tasks, deadlines, and appointments |
| `notes:read`, `notes:write` | Notes and activity |
| `communications:read`, `communications:write` | Communications log |
| `time:read`, `time:write` | Time entries |
| `notices:read`, `notices:write` | USCIS notices and status checks |
| `billing:read` | Invoices and payments. The user also needs billing access. |

## Endpoints

Paths end with a slash.

| Path | Methods | Scope | Notes |
|---|---|---|---|
| `/api/v1/` | GET | any token | The token's user, scopes, and expiry; every endpoint's address; and the allowed values for choice fields. Use it to test a connection. |
| `/api/v1/users/` | GET | any token | Active staff, for `assigned_to` fields |
| `/api/v1/contacts/` | GET, POST | contacts | Filters: `q`, `status`, `stage`, `assigned_to`, `updated_since` |
| `/api/v1/contacts/<id>/` | GET, PATCH | contacts | |
| `/api/v1/matters/` | GET, POST | matters | Filters: `q`, `status`, `case_type`, `contact` (primary contact or party), `assigned_to`, `updated_since` |
| `/api/v1/matters/<id>/` | GET, PATCH | matters | Includes parties |
| `/api/v1/work-items/` | GET, POST | work | Filters: `kind`, `completed`, `matter`, `contact`, `assigned_to`, `scheduled_after`, `scheduled_before`, `updated_since` |
| `/api/v1/work-items/<id>/` | GET, PATCH | work | `is_completed` marks the item done |
| `/api/v1/notes/` | GET, POST | notes | Filters: `matter`, `contact`, `kind` |
| `/api/v1/notes/<id>/` | GET | notes | |
| `/api/v1/communications/` | GET, POST | communications | Filters: `matter`, `contact`, `channel`, `updated_since` |
| `/api/v1/communications/<id>/` | GET | communications | |
| `/api/v1/time-entries/` | GET, POST | time | Filters: `matter`, `contact`, `user`, `started_after`, `started_before` |
| `/api/v1/time-entries/<id>/` | GET | time | |
| `/api/v1/notices/` | GET, POST | notices | Filters: `matter`, `kind`, `receipt_number`, `open_responses`, `updated_since` |
| `/api/v1/notices/<id>/` | GET, PATCH | notices | |
| `/api/v1/status-checks/` | GET, POST | notices | Filters: `matter`, `receipt_number` |
| `/api/v1/invoices/` | GET | billing | Filters: `status`, `matter`, `contact`, `updated_since` |
| `/api/v1/invoices/<id>/` | GET | billing | Includes lines and payments |

`assigned_to` and `user` accept a user ID or `me`. Date filters take ISO 8601 values, such as `2026-03-02` or `2026-03-02T09:00:00Z`. A value without a time zone uses the installation's time zone.

## Reading

Records use the model's field names.
- **Linked records** appear as their IDs, the same values you send when writing. Contacts, matters, notices, and communications have UUIDs; users, work items, notes, and time entries have whole-number IDs.
- **Money** is a string with two decimals, such as `"1500.00"`.
- **Times** are ISO 8601 with a time zone offset.
- **`web_url`** on contacts, matters, and invoices opens the record in the CRM.

Lists return one page of results and the address of the next page:

```json
{"results": [...], "next": "https://crm.example.test/api/v1/contacts/?limit=50&offset=50"}
```

`limit` defaults to 50 and can be at most 200. `next` is `null` on the last page.

To keep another system in step, poll with `updated_since` set to the time of your last poll. Results then come back oldest change first.

## Writing

POST creates a record and PATCH changes one. Send a JSON object with `Content-Type: application/json`. Bodies are limited to 256 KB.
- **PATCH changes only the fields you name.** Every other field keeps its value.
- **New records** get the same defaults the web form uses. `assigned_to` defaults to the token's user.
- **Rejected fields.** A field that is unknown or read-only, such as `id` or `matter_number`, is rejected rather than ignored.
- **Same side effects as the web pages.** A new contact or matter gets a "created" activity. A stage or status change is noted on the record. A notice's response due date becomes a deadline. A status check that differs from the last one is noted on the matter.
- **Audit entries.** Each one names the token's user and says the change came through the API token.

Resource-specific rules:
- **Notes and communications** belong to a `contact` or a `matter`. Given only a matter, they go to its primary contact.
- **Time entries** need `started_at` and either `stopped_at` or `duration_minutes`. They are logged for the token's user. Running timers stay in the web app.
- **Communications** accept an optional `external_id`. A second POST with the same `external_id` returns the first record with status 200 instead of creating a copy, so retried automations are safe.
- **Notices and status checks** need a `matter`. Receipt numbers may include spaces or dashes.

```bash
curl -X POST https://crm.example.test/api/v1/communications/ \
  -H "Authorization: Bearer $CRM_TOKEN" -H "Content-Type: application/json" \
  -d '{"channel": "call", "direction": "inbound", "contact": "<contact id>",
       "duration_minutes": 12, "summary": "Asked about the interview date.",
       "external_id": "phone-system-call-8812"}'
```

## Errors

Errors return a status code and a JSON body:

```json
{"error": {"code": "invalid", "message": "Some fields are not valid.", "fields": {"email": ["Enter a valid email address."]}}}
```

| Status | Codes |
|---|---|
| 400 | `invalid`, `unknown_fields`, `invalid_json`, `invalid_parameter` |
| 401 | `unauthorized`, `invalid_token`, `token_revoked`, `token_expired`, `user_inactive`, `two_factor_required` |
| 403 | `api_disabled`, `staff_tokens_off`, `missing_scope`, `no_billing_access` |
| 404 | `not_found` |
| 405 | `method_not_allowed` (the `Allow` header lists the methods) |
| 413, 415 | `too_large`, `unsupported_media_type` |
| 429 | `rate_limited` (retry after the `Retry-After` seconds) |

## Limits and the request log

Each token can make `API_HOURLY_LIMIT` requests an hour (default 1000).

Every request is logged, including refused ones. The log records the token, user, method, path, status, duration, and client address.
- It does not store query strings, because searches can contain client names.
- Request bodies are not stored.
- Superusers see the latest requests on the API access page; staff see their own. The full log is under Administration.

Responses are sent with `Cache-Control: no-store`. The API sends no CORS headers, so web pages on other sites cannot call it from a browser.
