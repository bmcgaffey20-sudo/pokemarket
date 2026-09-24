# Listing photo gallery

Deploy this backend before Android 0.22.2. The listing preview endpoint now supports
each saved photo label and original-photo viewing. It only signs photos belonging
to the requested published listing; arbitrary object keys are not accepted.

Existing listings require no rescan. Thumbnails are generated and cached as before.
Full originals are preserved for zooming and AI scans. Includes all prior features.
