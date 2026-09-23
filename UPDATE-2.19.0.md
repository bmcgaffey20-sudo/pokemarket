# PokeMarket 2.19.0 — transaction feedback and listing messages

Deploy these backend files before installing Android 0.22.0. Existing accounts,
orders, listings, camera capture and listing-photo updates remain included.
The new database tables are created automatically on startup; no reset is needed.

## Features

- Buyers and sellers can each leave one 1–5 star rating and a comment per completed
  or refunded transaction. Pending purchases and unresolved returns cannot be rated.
- Seller feedback is available from a published listing, with an average rating,
  count and paginated comments. Account shows feedback received as buyer or seller.
- Message user on a published listing starts or reopens a private listing conversation.
  Account → Messages shows conversations across Pokémon, Magic and Sports.
- Text messages, one optional photo per message, message retry deduplication,
  pagination, manual refresh, user blocking/unblocking and FCM notification support.
- Original JPEG, PNG and WebP photos up to 20 MB / 40 megapixels. Direct uploads
  bypass Render; the server reads each attachment once to validate the image.
  No quality-reducing re-encoding is performed.
- Only conversation participants can request photo links. Links expire after 10 minutes.
  Sent photos use a new object key so the original upload link cannot overwrite them.
- Unused upload tickets and objects older than one day are cleaned by the existing
  operational worker. Sent messages/photos are retained, including after a listing
  is removed, so the conversation remains available to its participants.

## Required photo setup

1. In Cloudflare R2 create a separate bucket, for example `pokemarket-messages`.
2. Keep Public Development URL and custom-domain public access disabled for it.
3. Give the existing R2 API credentials object read/write access to this bucket as
   well as the existing listing bucket. Keep the current R2 endpoint and credentials.
4. In Render → Environment add `R2_MESSAGE_BUCKET_NAME=pokemarket-messages`
   (substitute the exact bucket name), then redeploy.
5. Add an R2 lifecycle rule for the `messages/` prefix to expire objects after one
   day. This also removes abandoned staging objects if the free Render service sleeps.
   Do NOT apply this rule to `message-photos/`, which contains sent photos.

Text messaging and feedback work without this setting; photo uploads show a clear
configuration error until it is set. The native Android upload needs no browser CORS
configuration. No new Firebase credentials are required.

## Focused verification

Use two accounts and one unrelated third account. Start a conversation from a
published listing, reply from Account → Messages, attach a photo, refresh and verify
the original is readable. Confirm a block prevents both sides sending and only the
blocker can undo that block. Verify new-message notifications open Messages.
Use a completed test order to leave feedback once on each side; confirm duplicate
submissions fail and seller feedback appears on another published listing.

Server tests cover privacy, attachment ownership/validation, feedback eligibility,
duplicate prevention, blocking and paging. Android Studio build and device testing
remain required; Gradle download was unavailable in the build environment.
