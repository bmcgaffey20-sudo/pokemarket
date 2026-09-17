# Mailjet — backend 2.7.1

Mailjet replaces Resend for verification and password-reset emails. Existing
accounts, sessions, listings and R2 photos are preserved. No new dependency is
required: delivery uses the existing httpx client and Mailjet Send API v3.1.

## Render setup

In Mailjet, obtain your API Key and Secret Key from API key management. Validate
the sender address in Mailjet under that same API key. Use an active validated
sender; creating an account alone does not validate every From address.

Set these in the Render backend service's Environment:

    MAILJET_API_KEY=<Mailjet API Key>
    MAILJET_SECRET_KEY=<Mailjet Secret Key>
    EMAIL_FROM=PokeMarket <your-validated-sender@example.com>
    PUBLIC_BASE_URL=https://pokemarket-4jwi.onrender.com

Replace the sender placeholder with your actual validated address. Paste values
without surrounding quotes. Never put keys in Android, GitHub or chat. Keep
AUTH_SECRET, DATABASE_URL, R2 and AI settings unchanged. RESEND_API_KEY is no
longer used and can be removed. SMTP configuration is not needed.

Upload the extracted backend files to the existing GitHub backend repository and
deploy Render. Health should report version 2.7.1-mailjet and email configured.
That field confirms configuration presence, not successful email delivery.

## Test

1. In Android Account, request verification. Open the received link and confirm.
2. Return to Account and refresh verification status.
3. Test Forgot Password, then sign in with the new password. Old sessions should
   stop working. Reusing a consumed reset link should fail.
4. Confirm drafts, publishing and withdrawing still work.

If delivery fails, check spam, Mailjet message logs, sender activation and account
sending limits, then request another email. Sends are background tasks without a
durable retry queue. A successful request does not guarantee inbox delivery.
Click/open tracking is disabled for account-action messages. Provider secrets,
response bodies and account-action links are not written to application logs.

The current Android account UI works without a provider-specific change. The
accompanying project is version 0.11.1-mailjet (108). Do not uninstall your app;
build an update using the same signing key to preserve local drafts.

Validation: 30 backend tests passed with mocked mail and SQLite; live Mailjet,
Neon and device installation still require testing. Android syntax is checked,
but a full Android build has not been verified in this environment.

Official API reference:
https://dev.mailjet.com/docs/email-api/send-api-v31/send-basic-email
