# Google Drive

Copies each matter's documents, one way, into a folder for the matter on the firm's Google Drive. It is in Beta: its tests run against a fake Drive that answers as Google's documentation describes, but it has not run against a live Drive yet.

## What it does

- **A folder per matter.** Inside the folder the firm chooses, ideally on a shared drive, the CRM makes a folder for each matter, named with the matter's number and title, and renames it when the matter is renamed.
- **A copy of each document.** A document added to a matter is copied into its folder. Documents from before the firm connected, and any copy that failed, are copied by a run every 15 minutes, 25 at a time; the runs show on the connection's page as Document storage syncs.
- **Deleting.** Deleting a document or matter in the CRM moves its copy to Drive's trash, where it stays for 30 days before Drive removes it.
- **One way.** The CRM stays the record. Nothing is read back from Drive, so changes made in Drive are not copied into the CRM, and the CRM never changes or removes anything it did not make. Each folder and file the CRM makes carries the CRM's ID in a private property only the firm's app can read.

Every copy goes through the outbound policy. A new connection starts in preview mode, so nothing is copied until preview mode is turned off; the documents added meanwhile are then copied by the next runs.

## Setting it up

1. **A Google Cloud app for the firm.** In the Google Cloud console, in a project owned by the firm's Google Workspace organization, enable the Google Drive API. Under **Google Auth Platform**, set the audience to **Internal** and add the scope `https://www.googleapis.com/auth/drive`. Create an **OAuth client ID** of type **Web application** with the redirect URI shown on the connector's page in the CRM.
2. **The account to connect.** Copies are made by the account that signs in, and the `drive` scope lets that account's token reach everything that account can see in Drive. Use a dedicated Workspace account for the CRM that can see only the firm's shared drive for client documents, with the **Content manager** role there, which it needs to move copies to the trash.
3. **The folder.** In that shared drive, make a folder for matter folders and copy its link.
4. **Connect.** In the CRM, someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) opens **Connections > Google Drive**, enters the app's client ID and secret, pastes the folder's link, and signs in as the dedicated account. The CRM checks that it can add files to the folder and move them to the trash.
5. Someone who manages connections and AI reviews what the connection sends and receives, and turns preview mode off.

If the firm's Workspace admin limits which apps can use Google Drive, the admin marks the app as trusted, or allows trusted internal apps, in the Admin console.

## Why the `drive` scope

Google's narrower `drive.file` scope only reaches files an app made itself or that someone picked with Google's Picker. The CRM would then need Google's Picker scripts on its pages to use the folder the firm chose, which its content security policy does not allow. The `drive` scope is restricted, but an Internal app on the firm's own Workspace needs no Google verification for it. The dedicated account in step 2 keeps what the token can reach to the one shared drive.

## Limits

- Files up to the CRM's own upload limit (20 MB unless the firm changed it) are uploaded as resumable uploads.
- Drive's API quotas are far above what a firm's documents need; a request Drive refuses is tried again by a later run.
