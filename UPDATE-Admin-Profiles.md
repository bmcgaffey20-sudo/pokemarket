# Admin profiles and message reporting
Backend 2.26.0-admin-profiles; Android 0.29.0-admin-profiles, versionCode139.

Admin > Users is now a compact searchable directory with 10/20/30/40/50 users per page, previous/next and direct page jump. Search resets pagination. Each user opens an admin profile with email, Stripe Connect onboarding/charge/payout readiness, account age and account health. Stripe lookup failures show unknown; no linked account shows not connected. Seller-tier and restriction controls remain in the profile.

Message history lists paginated conversations; each opens sender names, timestamps, message text and photo attachments. Purchase history lists this user's purchases, with each opening the existing full sale details and audit history. All investigation APIs require admin authorization. Ordinary users cannot access these profiles. Regular users retain access to their own Account Health page.

Received messages include Report message with a reason. Reports are deduplicated per reporter/message, reject self-reporting and require conversation participation. Admin > Reported messages lists reports, captured message text/reason, and opens the conversation. Mark reviewed does not ban anyone or delete the message; moderation remains on the user's admin profile. Report snapshots can remain after original conversations are deleted; attachment availability depends on cloud retention. Privacy policy now discloses report records. Reports are visible only to administrators.

Deployment: upload the complete backend contents together; new message_reports table is created automatically at startup. No new credentials or environment variables are required. Keep existing Stripe webhook dispute events from the Account Health update. Rebuild Android with JDK17; retain local.properties and app/google-services.json. Existing app/backend features remain included; no EasyPost integration.

Validation: 106 backend tests pass. Android source reviewed but not compiled here because the Android SDK is unavailable. Build and install in Android Studio, then check user pagination/search, admin profiles and both history tabs, report from the other test account, inspect report and mark reviewed.
