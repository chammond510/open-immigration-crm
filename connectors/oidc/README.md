# OpenID Connect sign-in

Lets staff sign in with the firm's identity provider, such as Google Workspace or Microsoft Entra ID, or any other provider that supports OpenID Connect, such as Okta or JumpCloud. It is in Beta: its tests run against a fake provider that signs real ID tokens, but it has not run against a live provider yet.

## What it does

- **A button on the sign-in page.** Each sign-in connection adds a **Sign in with ...** button, named after the connection's label. Usernames and passwords keep working beside it, so a superuser can always get in if the provider is down or misconfigured.
- **Only existing staff accounts.** No account is ever created. The first time someone signs in, the CRM links their identity to the one active staff account with the same email address, but only if the provider vouches for that address. If no account matches, or more than one does, they are refused. After that, the link holds by the provider's own ID for the person, so a changed email address does not move it. Superusers can see and delete links under **Administration > Sign-in links**; a deleted link is made again the next time the person signs in.
- **The firm's domains only.** Every sign-in must be from an email address in one of the firm's domains. For Google Workspace, the account must also belong to the firm's Workspace (its `hd` claim).
- **Two-factor sign-in.** After the provider, the CRM's own two-factor step still follows for staff who have it set up, and the firm's "require two-factor sign-in" setting still applies. A connection can be set to skip the CRM's step when the provider reports that the person used a second factor (the `amr` claim includes `mfa`). Microsoft Entra ID reports this once the `amr` optional claim is added. Google usually does not, so for Google the CRM's own step stays.
- **Checks.** The sign-in uses the authorization code flow with PKCE (S256), a one-time state, and a nonce, and expires after ten minutes. The ID token's signature is checked against the provider's published keys, along with its issuer, audience, expiry, issue time, and nonce. Every sign-in, refusal, and failure is in the audit log.

## The return address

Every provider asks for a redirect URI. Use the address shown on the connector's page in the CRM, which is the CRM's own address followed by `/accounts/sign-in/callback/`, for example `https://crm.example.test/accounts/sign-in/callback/`. It must match exactly, and the CRM must be served over HTTPS.

## Google Workspace

1. In the Google Cloud console, under **Google Auth Platform**, set the app's audience to **Internal**, so only the firm's Workspace accounts can use it.
2. Create an **OAuth client ID** of type **Web application**, and add the return address as an authorized redirect URI.
3. In the CRM, open **Connections > OpenID Connect**, enter the client ID and secret, choose **Google Workspace**, and enter the firm's Workspace domain first under **Staff email domains**, followed by any other domains staff addresses use.

## Microsoft Entra ID

1. In the Microsoft Entra admin center, under **App registrations**, register an app for **Accounts in this organizational directory only**, with a **Web** redirect URI set to the return address.
2. Under **Certificates & secrets**, create a client secret. Secrets last at most 24 months; note the date and connect again with a new secret before it ends.
3. Under **Token configuration**, add the optional claims **email** and **xms_edov** to the ID token, and **acct** if you like. The CRM trusts an email address from Entra only when `xms_edov` says Microsoft verified its domain, and refuses guest accounts. To let the provider's second factor count, also add **amr**.
4. In the CRM, choose **Microsoft Entra ID**, enter the client ID (the Application ID) and secret, the **Directory (tenant) ID** from the app's overview page, and the staff email domains.

Entra identifies each person by their object ID within the firm's tenant, so registering a new app later does not break existing links.

## Other providers

Create an OpenID Connect **web application** with the authorization code flow, a client secret, and the return address. In the CRM, choose **Another OpenID Connect provider** and enter its issuer exactly as its discovery document (`/.well-known/openid-configuration`) gives it. The provider must send `email` and `email_verified` in the ID token, with the `openid email profile` scopes.

## Before relying on it

Sign in once with a staff account of each kind the firm uses, including a superuser, and check the link under **Administration > Sign-in links**. Keep at least one superuser with a password and the CRM's own two-factor sign-in, for when the provider cannot be reached.
