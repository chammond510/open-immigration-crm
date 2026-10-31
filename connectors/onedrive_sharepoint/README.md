# OneDrive and SharePoint

Copies each matter's documents, one way, into a folder for the matter in the firm's SharePoint or OneDrive, through Microsoft Graph. It is in Beta: its tests run against a fake Graph that answers as Microsoft's documentation describes, but it has not run against a live tenant yet.

## What it does

- **A folder per matter.** Inside the folder the firm chooses, ideally in a SharePoint document library the firm owns, the CRM makes a folder for each matter, named with the matter's number and title, and renames it when the matter is renamed.
- **A copy of each document.** A document added to a matter is copied into its folder. Documents from before the firm connected, and any copy that failed, are copied by a run every 15 minutes, 25 at a time; the runs show on the connection's page as Document storage syncs. Large files are uploaded in pieces.
- **Names.** If a name is already taken in the folder, the copy gets a number added rather than replacing anything. Names SharePoint refuses, such as `CON` or ones starting with `~`, are adjusted.
- **Deleting.** Deleting a document or matter in the CRM moves its copy to the site's recycle bin, from which it can be restored.
- **One way.** The CRM stays the record. Nothing is read back, so changes made in SharePoint are not copied into the CRM, and the CRM never changes or removes anything it did not make.

Every copy goes through the outbound policy. A new connection starts in preview mode, so nothing is copied until preview mode is turned off; the documents added meanwhile are then copied by the next runs.

## Setting it up

1. **An app registration for the firm.** In the Microsoft Entra admin center, under **App registrations**, register an app for **Accounts in this organizational directory only**, with a **Web** redirect URI set to the address shown on the connector's page in the CRM. Under **Certificates & secrets**, create a client secret; note when it expires.
2. **Permissions.** Under **API permissions**, add the Microsoft Graph delegated permissions `offline_access` and `Files.ReadWrite.All`. They do not require an administrator, but the firm's consent settings may, so an administrator can grant consent for the firm.
3. **The account to connect.** Copies are made by the account that signs in, and the permission lets that account's sign-in reach every file the account can reach. Use a dedicated Microsoft 365 account for the CRM that is a member, with edit rights, of only the SharePoint site for client documents.
4. **The folder.** In that site's document library, make a folder for matter folders and copy its link with **Copy link**. Microsoft documents this kind of link for finding a folder; an address copied from the browser's address bar may not work.
5. **Connect.** In the CRM, a superuser opens **Connections > OneDrive and SharePoint**, enters the app's client ID, secret, and directory (tenant) ID, pastes the folder's link, and signs in as the dedicated account. The CRM checks that the link leads to a folder. Graph cannot say ahead of time whether the account may add files there, so if it cannot, the first copy fails and says so on the connection's page.
6. A superuser reviews what the connection sends and receives, and turns preview mode off.

## Why `Files.ReadWrite.All`

`Files.ReadWrite` reaches only the signed-in person's own OneDrive, not a SharePoint library. Microsoft's narrower "Selected" permissions need an administrator to grant the app access to the folder through Graph, which this connector does not offer yet. The dedicated account in step 3 keeps what the sign-in can reach to the one site.

## Limits

- Files up to 4 MiB are uploaded in one request; larger ones in pieces of about 3 MiB.
- Microsoft limits how fast an app may call Graph; a refused request is tried again by a later run.
