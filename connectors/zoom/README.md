# Zoom

Makes a Zoom meeting for each of a staff member's appointments, on their own Zoom account, and keeps the meeting's join link on the appointment. It is in Beta: its tests run against a fake Zoom that answers as Zoom's documentation describes, but it has not run against a live Zoom account yet.

## What it does

- **A meeting for each appointment.** When an appointment with a time is assigned to someone who connected Zoom, the worker makes a scheduled meeting on their account, with the appointment's title as its topic, and keeps the join link on the appointment. The link shows on the work list and on the contact's and matter's pages, and in the description of the appointment on the staff member's connected calendar. Staff share it with the client themselves; the CRM sends nothing to the client.
- **Kept in step.** Changing the appointment's title or time updates the meeting. Unscheduling it, giving it to someone else, or deleting it deletes the meeting, without emailing anyone. A finished appointment keeps its meeting. If the meeting was deleted in Zoom, the next change makes a new one.
- **Only the title and time leave the CRM.** No client names, matter numbers, or notes are sent, unless the appointment's title contains them. Meeting settings, such as the waiting room and passcode, follow the staff member's Zoom settings.
- **Links staff paste.** An appointment can also be given a meeting link by hand, such as another service's. The CRM never replaces a link it did not make.
- **What is kept.** Only the meeting's ID and join link. The host's start link, which signs in as the host, is never stored.
- **Limits.** Zoom allows 100 meeting creations or changes a day for each person, counted from midnight UTC, so the CRM updates a meeting only when its title or time changes.

Every request goes through the outbound policy. A new connection starts in preview mode, so no meeting is made until preview mode is turned off.

## Setting it up

1. A Zoom account admin creates the firm's app in the Zoom App Marketplace (**Develop > Build App**): a **General app**, **user-managed**. Keep it unpublished, or publish it to **Users within my Zoom account** only. An unpublished app works only for members of the firm's own Zoom account.
2. Under the app's OAuth settings, add the CRM's redirect URL, shown on the connector's page in the CRM: the CRM's own address followed by `/connections/oauth/callback/`.
3. Under **Scopes**, add `meeting:read:meeting`, `meeting:write:meeting`, `meeting:update:meeting`, and `meeting:delete:meeting`.
4. In the CRM, someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) opens **Connections > Zoom** and enters the app's client ID and client secret.
5. Each staff member then connects their own Zoom account from the same page and signs in to Zoom. Someone who manages connections and AI reviews what the connection sends and receives, and turns preview mode off when the firm is ready.

Zoom does not tell an unpublished app when someone removes it; the connection shows that it needs a new sign-in the next time the CRM uses it.
