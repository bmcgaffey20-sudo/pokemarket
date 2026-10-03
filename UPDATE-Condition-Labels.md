# Condition labels and instant-offer request fix
Backend 2.27.1-instant-sales; Android 0.30.1-instant-sales (versionCode141).

Condition categories, confidence, evidence and defect labels now render in bold followed by a dash, with normal-weight observation text. Applies to saved listing analysis, scan results and labelled public listing descriptions. Stored seller descriptions are not rewritten.

Sell Instantly requests safely URL-encode the cloud listing ID. Backend now resolves older owned scan IDs to the canonical listing ID, while denying another user's IDs. A missing-route 404 explains that the backend deployment must finish rather than showing a generic Not Found. Actual owned-listing errors retain their specific message. Public health and OpenAPI checks confirmed version 2.27.0 with instant-offer routes available during investigation; the screenshot's generic Not Found can occur before a deploy finishes. No authenticated live quote was attempted, so no production Gemini call or payment was made.

Deploy the full backend, rebuild Android with JDK17, preserve app/google-services.json and local.properties, and wait for backend health version 2.27.1-instant-sales before testing. Reopen the published card from My Cards after cloud sync. All existing pricing rules, admin-only valuation information, zero instant-sale marketplace fee and standard funded order/hold flow remain unchanged.

Validation: 115 backend tests passed. Android source checked; Android SDK is not available here for compilation. If an offer still fails after updating, capture its exact error and the matching Render POST log; provider availability cannot be established without a real quote request.
