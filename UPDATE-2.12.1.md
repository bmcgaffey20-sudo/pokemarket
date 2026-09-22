# PokeMarket Backend 2.12.1 — legacy deletion compatibility

- Resolves an older phone card ID against either the cloud listing ID or its unique scan ID.
- Keeps ownership and order-history protections in place.
- Continues removing every linked R2 object before deleting the database listing.

Deploy this backend before installing Android 0.15.2.
