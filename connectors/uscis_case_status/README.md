# USCIS Case Status API

Checks the case status of the receipt numbers on open matters once a day, through the firm's own app at [developer.uscis.gov](https://developer.uscis.gov). It is in Beta: the fake USCIS in its tests follows USCIS's published API documentation, but it has not run against the live API yet.

## What it does

- **Which receipt numbers.** It checks each receipt number on a matter that is not closed, whether the number is on the matter itself or on one of its notices. A receipt number on more than one matter is skipped, because its status could not be filed on the right one.
- **How often.** Each receipt number is checked about once a day. The worker runs the connector every hour and checks only what is due, at most 300 receipt numbers per run.
- **Pace.** Requests go out two a second, under USCIS's limits. When USCIS answers with its rate limit, the run stops and keeps what it has; the rest are checked in a later run. Three receipt numbers in a row without an answer also stop the run.
- **What staff see.** Each reading appears in the matter's case status. A change in status is noted on the matter and creates a review task for the matter's assigned staff member. If nobody is assigned, the contact's assigned staff member gets it. The task's priority follows the kind of change. A request for evidence or a denial is urgent. An interview, biometrics, transfer, or approval is high. Routine progress, such as "Case Is Being Actively Reviewed", is noted without a task.
- **What it never does.** It does not interpret a change or tell the client anything. The task asks staff to review the update first.
- **When USCIS cannot answer.** The matter shows it until a status is recorded. USCIS answers "not found" both for a number it does not recognize and for cases protected under 8 U.S.C. 1367 (VAWA, T, and U). Its API never shows those cases.

## Setting it up

1. Sign up at developer.uscis.gov and create an app with the **Case Status API** product. USCIS issues a sandbox client ID and secret.
2. In the CRM, open **Connections**, then **USCIS Case Status API**. Paste the client ID and secret, and leave **Environment** on **Sandbox**. A superuser reviews what the connection sends and receives, then connects. The connection test gets an access token and checks one of USCIS's test receipt numbers.
3. Make sure the worker is running (`python manage.py run_worker`).

## The sandbox

The sandbox answers only for USCIS's published test receipt numbers. In the sandbox, the connector checks five numbers once a day, never client receipt numbers:

- four published test numbers;
- one number in the test range that USCIS does not publish, so the "not found" answer is covered too.

To see sandbox readings in the CRM, put a test receipt number, such as `EAC9999103403`, on a fictional test matter.

The sandbox is open on weekdays from 7 AM to 8 PM Eastern. It allows 1,000 requests a day and 5 a second.

## Production access

USCIS asks for at least five consecutive calendar days of sandbox traffic, with both successful (200) and error (4xx) answers, before it considers production access. The daily sandbox check provides that traffic.

1. When the sandbox connection has run for at least five days, email USCIS Torch API Developer Support at developersupport@uscis.dhs.gov to request production access.
2. USCIS reviews the firm's app and may ask for a demo. When it grants access, it issues production credentials and confirms the production address.
3. Connect again with **Environment** set to **Production**, the production client ID and secret, and the production address (`https://api.uscis.gov` unless USCIS gives another). The address must be a `uscis.gov` HTTPS address.
4. Disconnect the sandbox connection.

In production, USCIS allows 400,000 requests a day and 10 a second.

## Errors

USCIS asks apps to show its error messages. A problem with one receipt number appears on the matter with USCIS's message. A problem with the connection, such as a rejected token or missing access to the Case Status API, appears on the connection's page with USCIS's message, after any receipt numbers in it are removed.

The limits and steps above are from developer.uscis.gov as of October 30, 2026.
