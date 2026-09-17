# Account recovery — backend 2.7.0

Historical release notes: email setup below is superseded by UPDATE-2.7.1.md.
Do not configure Resend for the current release.

## Setup (backend first)

Keep all existing Render secrets, including AUTH_SECRET. Do not regenerate them.
This update uses the Resend HTTPS email API. Create a Resend account, add a domain
you control, and add the DNS records Resend provides. Verify the domain, then
create a sending-only API key, preferably scoped to that domain.

Add these variables in Render -> pokemarket -> Environment:

    RESEND_API_KEY=<your Resend sending API key>
    EMAIL_FROM=PokeMarket <accounts@your-verified-domain.com>
    PUBLIC_BASE_URL=https://pokemarket-4jwi.onrender.com

The sender above is a placeholder: use your own verified domain. Do not put the
key into GitHub, Android code, or chat. The public URL must be your actual HTTPS
backend origin; links use this configured origin, never the request Host header.
Resend's default test sender only delivers to your own Resend account email;
it cannot verify arbitrary users until you configure a verified domain.

Official setup references:
- https://resend.com/docs/api-reference/emails/send-email
- https://resend.com/docs/api-reference/errors
- https://resend.com/docs/api-reference/api-keys/create-api-key

Upload every extracted backend file (including recovery.py and account_action.html)
to your GitHub repository root and deploy Render. Health should report
2.7.0-account-recovery, database connected, auth configured, email configured.
The email health field checks configuration presence; it does NOT prove delivery.

The additive database migration preserves accounts, passwords, photos and listings.
Existing accounts start unverified. Existing sessions remain valid until expiry or
a password reset. Verification is informational in this release; it does not
retroactively withdraw listings or block your working publish tests.

## Features and endpoints

- Registration schedules an email when mail is configured; existing users can
  request one via POST /api/v1/auth/send-verification (bearer token required).
- POST /api/v1/auth/forgot-password accepts {"email":"..."} and always returns
  the same 202 response for existing and nonexistent accounts.
- POST /api/v1/auth/verify-email accepts {"token":"..."}.
- POST /api/v1/auth/reset-password accepts {"token":"...","password":"..."}.
- GET /account/verify and /account/reset serve the browser confirmation forms.
- GET /api/v1/auth/me now includes email_verified.

Links contain a 256-bit random token in the URL fragment, which is not sent in
HTTP requests or normal server access logs. The browser removes it from history
and sends it in a POST only after the person confirms. Passwords and tokens are
never echoed in validation responses. Only token SHA-256 digests are stored.
Reset tokens expire after 30 minutes; verification tokens after 24 hours.
Redeeming a token is single-use and transactional. Reset increments the account's
session version and invalidates all outstanding account-action tokens. It does
not itself set email_verified; request a fresh verification email afterward.

Rate limits use atomic PostgreSQL counters, with SQLite support for tests. Per
15-minute window: login 10/account and 50/IP; registration 5/email and 25/IP;
recovery 3/email and 15/IP; verification 3/account and 15/IP; token submission
50/IP shared between verification and reset. Client-supplied forwarding headers
are not read here; deploy behind the trusted Render proxy. Verify actual client
IP handling before launch, since a shared proxy address can share IP limits.

Email sends run as short background tasks with a timeout. There is no durable
mail queue or automatic retry in this version. Deployment interruption, sender
configuration or provider quota may prevent delivery: resend from the app after
checking the provider dashboard. Logs show generic provider status, never keys,
tokens, recipient addresses or message content. A resend does not invalidate an
earlier still-valid link; completing the action invalidates sibling links.

## Verify after deployment

1. Account -> Send Verification Email; open the email link and press Confirm.
2. Return to Account -> Refresh Verification Status; expect Email verified.
3. Sign out. Enter your email and tap Forgot Password. Open the email link,
   enter matching new passwords, and submit. Never send the link to anyone else.
4. Sign in with the new password. The old password and any old session must fail.
5. Reopen the used link: it must not work again. Request fresh links as needed.
6. Confirm your drafts, Browse, Publish and Withdraw still work.

Automated tests use SQLite and mocked email delivery. Live Neon/R2/email delivery
and the Android device build must still be verified on your deployment.
