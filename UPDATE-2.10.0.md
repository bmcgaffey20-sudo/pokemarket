# PokeMarket Backend 2.10.0 — bandwidth optimization

- Combines identification, condition, and authenticity into one Gemini image request.
- Limits Gemini to the configured model plus one controlled fallback.
- Adds authenticated direct-upload sessions so original photos travel from Android
  directly to private Cloudflare R2 instead of passing through Render.
- Verifies direct-upload object paths, byte counts, MIME types, ownership, and the
  required four-angle evidence contract before committing database records.
- Keeps the legacy multipart upload endpoint for compatibility during rollout.

Deploy this backend before installing Android 0.14.0.
