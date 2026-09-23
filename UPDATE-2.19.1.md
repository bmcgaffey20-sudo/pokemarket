# Listing thumbnail fix

Deploy backend 2.19.1 before installing Android 0.22.1.

Published listing rows now request a dedicated JPEG preview of the selected
top-center photograph, fitted within 640 × 640 pixels at JPEG quality 92.
The first request generates the preview and saves it in the existing listing R2
bucket. Later requests reuse it. Existing listings need no rescan or republication.

Full-resolution originals remain unchanged for AI scanning and listing detail
photos. Thumbnail work is serialized to limit server memory usage. Deleting or
replacing an original also removes its cached preview.

No additional environment variables are needed for listing thumbnails. This
release includes 2.19.0 feedback and messaging; the separate private message-photo
bucket setup still applies only to messaging attachments.

The Android thumbnail now shows a loading indicator and a Retry photo button on
failure, with longer network timeouts for initial generation/free-server wakeup.
Signed-link refreshes reuse the app image cache instead of downloading again.

Validation covers top-center selection, unpublished listing access, cached preview
reuse, JPEG dimensions, original preservation and thumbnail deletion. Android build
and device verification are still needed because Gradle download was unavailable.
