# PokeMarket Backend 2.11.0 — queued scans

- Adds a durable PostgreSQL scan-job queue with one memory-bounded worker.
- Lets web requests return immediately while Gemini analysis runs in the background.
- Reuses private R2 listing originals for analysis instead of receiving a second
  multipart copy through Render.
- Preserves the old multipart scan endpoint for older Android builds.
- Sets explicit SQLAlchemy pool limits suitable for a small Render/Neon deployment.

Deploy this backend before installing Android 0.15.0.
