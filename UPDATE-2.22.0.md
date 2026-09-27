# Slab Grade / PokeMarket 2.22.0 — Account controls and retained transactions

Deploy this backend before Android 0.25.0. Keep the existing database and environment variables. Do not delete the database or its tables. This release creates four additive tables: account_controls, account_control_audit, retained_transactions, account_deletion_objects. Back up your PostgreSQL database using your provider before deployment.

## Features

- Admin > Users > Account controls: 7-day suspension, 30-day suspension, indefinite ban, restore. Reason and explicit confirmation required. Action, actor and timestamp recorded. Admin accounts cannot be restricted through this UI.
- Restrictions apply to current sessions on the next request. Existing order fulfillment, returns, payout onboarding and account deletion remain accessible; new listings, scans, checkouts and messaging are blocked. Published listings are withdrawn. Expired suspensions restore activity automatically; listings are not automatically republished.
- Account > Privacy & account deletion: password reauthentication and typed DELETE confirmation. Authenticated web alternative at /account-deletion. Public policy at /privacy. Administrators must remove their ADMIN_EMAILS entry and transfer administrative responsibility before deleting their account.
- Deletion is deferred for pending checkouts, unresolved orders/returns/refunds, unpaid payouts and in-flight scans. Requests stay queued and finalization retries through the existing settlement worker. A stuck checkout or provider operation needs operator investigation, not automatic loss of transaction data.
- Completed deletion tombstones the user to preserve financial foreign keys, disables credentials/sessions, removes personal listing content, photos metadata, feedback, entire participant conversations and notification tokens. Device-local cards are removed on the requesting Android device; other devices/backups cannot be remotely erased by this release.
- R2 cleanup has a durable retry queue. It waits at least 65 minutes (or the configured signed-link lifetime plus five minutes) so an outstanding upload cannot immediately recreate deleted content. It includes a listing-originals prefix sweep and derived thumbnails. R2 credentials require list/delete access to the relevant buckets. Pending cleanup is visible through account-control status.
- Admin > Ledger displays retained financial records. The ledger is a dedicated table in the existing PostgreSQL database, NOT an independent backup or new paid service. It has no cascading foreign key to users/listings. Internal IDs and payment references remain pseudonymous, not anonymous. No names, email, addresses, conversations or photos are stored in this ledger.
- Existing sales are backfilled, and changed orders refresh the ledger in bounded batches. Financial records have no automatic expiration in this release. Decide and document a financial-retention schedule with your accountant/counsel before broad public release. Provider database backups remain necessary.
- Weekly (Monday-to-Monday UTC) and calendar-month UTC CSV reports are sent to ADMIN_REPORT_EMAIL using existing Mailjet settings. Set ADMIN_REPORT_EMAIL=bmcgaffey20@gmail.com. Names, emails, addresses and message contents are excluded. Values ending in _cents are integer cents. Commissions are platform commissions, not separately reconciled processor fees. Reports are snapshots; later refunds/payouts update the ledger rather than rewriting previously emailed spreadsheets. Older emailed reports require separate privacy handling.
- The worker sends the most recently completed week/month when running, including empty reports. It does not backfill every missed historical email period. Render free-tier sleeping delays scheduled work until the service resumes; no paid Render service was added.
- Address retention now applies only to settled transactions, default 30 days after completion/last update, instead of stripping addresses from open disputes. Final eligible deletion removes transaction addresses sooner.

## Deployment

1. Back up PostgreSQL. Upload these backend files at the repository root (main.py must be at root). Do not upload Android files. Keep Render env values.
2. Confirm ADMIN_EMAILS contains only administrators and ADMIN_REPORT_EMAIL is correct. Keep existing Mailjet, R2, Stripe, database and Firebase configuration. Set APP_NAME=Slab Grade API if desired. This update does not rename applicationId, Stripe objects or existing infrastructure.
3. Deploy. Health must show 2.22.0-account-controls.
4. Check /privacy and /account-deletion on your backend domain. Review the policy's operator/contact, providers, free/paid Gemini data handling, backup/log practices and retention statements against actual configuration. This draft is not a guarantee of legal or Google Play compliance.
5. Install Android 0.25.0 and test using disposable accounts. Suspend one for 7 days, confirm new activity fails and existing orders remain accessible, then restore. Test deletion with an unpaid/open order and again after settlement. Check Admin > Ledger and cloud cleanup status.

## Validation

Backend regression and account-control tests run with local SQLite and mocked storage; no live Stripe, R2, Mailjet or production database operations are used. Android Gradle compilation could not run here because downloading Gradle was blocked by network access; build and test in Android Studio before distributing. The package retains the existing Android SDK/toolchain versions; Play target-SDK migration is a separate task.
