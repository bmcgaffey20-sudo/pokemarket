# Offer rates, presentation, and Discord update

Near Mint/Mint automatic offers: 20%. Lightly Played: 15%. Unanswered older automatic quotes refresh; accepted offers and admin counteroffers preserve agreed amounts. Existing review and damage rules remain.

Android: bold card names and condition labels, blank lines between condition categories, readable photo references, bold dates displayed in device local time, and Join Discord in the marketplace navigation menu: https://discord.gg/VCNDMn63kV.

Backend version: 2.27.2-instant-sales. Android version: 0.30.2-instant-sales (142). No new environment variables. Preserve your Android google-services.json and local.properties when replacing source files.

Backend test suite passes. Android source inspected; Android SDK unavailable here, so build in Android Studio with JDK 17. The reported account-check HTTP 500 requires a Render traceback if it persists; Stripe lookup failures are covered by a regression test.
