# Activity badges update

Deploy backend 2.21.0 before installing Android 0.24.0 (version code 132).
New notification and message-read tables are created during database initialization.
No new environment variables or Firebase configuration are required.
Keep your existing app/google-services.json and local.properties when updating Android.

The main header shows Notifications (bell) then Messages (envelope) on the right.
The Account page no longer has a Messages button. Badges show unread item counts,
hide at zero, and show 99+ above 99. Opening a conversation marks the displayed
latest messages read; tapping a notification marks it read and opens its destination.
Notifications also have a bulk read action through the displayed page.
Firebase foreground pushes refresh counts immediately, with a small 15-second
foreground-only fallback request. Resuming the app refreshes counts. Actual push
delivery depends on Firebase and network availability. The camera flow has no header.
New account/order notifications are persisted even without Firebase credentials.
Historical pushes cannot be backfilled. Existing unread conversation messages count.

Validation: 88 backend tests passed. Android compilation could not be verified in
this environment because downloading Gradle is blocked. Build in Android Studio
using your existing JDK 17 configuration. Quick device check: send a message from
a second account, confirm envelope count, open the chat and confirm it clears;
trigger an order notification, open bell, tap item, and confirm count clears.
