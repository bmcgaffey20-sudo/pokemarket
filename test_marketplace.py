import asyncio
import pytest
import main
from datetime import timedelta
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from auth import create_access_token
from database import Database, Listing, Order, normalize_rarity, rarity_from_ai_result, utc_now
from market_rules import price_tier
from main import app, require_database, require_user
from reporting import build_sales_csv
from tracking import TrackingValidationError, classify_tracking_number


@pytest.fixture
def market(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'market.db'}")
    database.initialize()
    user, _ = database.create_user("seller", "seller@example.com", "Seller", "hash", "salt", 10000)
    database.create_user("other", "other@example.com", "Other", "hash", "salt", 10000)
    database.update_seller_address("seller", {
        "name": "Seller", "line1": "1 Market Street", "line2": "", "city": "Pasco",
        "state": "WA", "postal_code": "99301", "country": "US",
    })
    app.dependency_overrides[require_database] = lambda: database
    app.dependency_overrides[require_user] = lambda: user
    class Storage:
        def presign_object(self, key):
            return "https://example.com/signed-photo"
    monkeypatch.setattr("main.get_r2_storage", lambda: Storage())
    database.upsert_listing("card", dict(title="Dracozolt VMAX", description="Condition disclosure", price_cents=1250, currency="USD", card_name="Dracozolt", estimated_condition="Near Mint", status="draft"), "seller")
    images = [dict(label="required_" + label, object_key="listings/card/" + label, content_type="image/jpeg", size_bytes=100) for label in ("front_straight", "front_slight_left", "front_slight_right", "back")]
    database.replace_images("card", images, "seller")
    try:
        yield TestClient(app), database, user, images
    finally:
        app.dependency_overrides.clear()
        database.engine.dispose()


def test_publish_browse_withdraw_and_privacy(market):
    client, db, _, _ = market
    assert client.get("/api/v1/marketplace").json() == []
    assert client.get("/api/v1/marketplace/card").status_code == 404
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    assert client.get("/api/v1/marketplace/top?limit=20").json()[0]["view_count"] == 0
    public = client.get("/api/v1/marketplace/card").json()
    assert public["title"] == "Dracozolt VMAX"
    assert public["seller"] == {"display_name": "Seller", "tier": 1}
    assert not {"seller_id", "scan_id", "ai_result", "email", "password_hash"} & public.keys()
    assert set(public["images"][0]) == {"label", "url"}
    assert public["view_count"] == 1
    assert public["rarity_tier"] == "rare"
    assert public["card_type"] is None
    assert client.get("/api/v1/marketplace/card").json()["view_count"] == 1
    assert client.get("/api/v1/marketplace/card", headers={"User-Agent": "second-viewer"}).json()["view_count"] == 2
    assert client.get("/api/v1/marketplace/card", headers={"X-PokeMarket-Viewer": "install-one"}).json()["view_count"] == 3
    assert client.get("/api/v1/marketplace/card", headers={"X-PokeMarket-Viewer": "install-one"}).json()["view_count"] == 3
    assert client.get("/api/v1/marketplace/top?limit=10").json()[0]["id"] == "card"
    assert len(client.get("/api/v1/marketplace?q=dracozolt").json()) == 1
    assert len(client.get("/api/v1/marketplace?q=dracozolt&limit=20").json()) == 1
    assert client.get("/api/v1/marketplace?q=missing").json() == []
    assert client.get("/api/v1/marketplace?offset=1").json() == []
    assert client.get("/api/v1/marketplace?limit=1000").status_code == 400
    assert client.post("/api/v1/listings/card/unpublish").status_code == 200
    assert client.get("/api/v1/marketplace/card").status_code == 404
    assert len(db.get_listing("card", "seller")["images"]) == 4


def test_top_viewed_hides_unviewed_and_exposes_verified_type(market):
    client, db, _, images = market
    db.upsert_listing("card", {"set_name": "Obsidian Flames", "ai_result": {
        "identification": {"card_type": "Water"},
        "tcgdex": {"types": ["Fire"]},
    }}, "seller")
    db.upsert_listing("second", dict(title="Other", description="Condition disclosure", price_cents=1350, currency="USD", card_name="Other", estimated_condition="Near Mint", status="draft"), "seller")
    db.replace_images("second", [dict(image, object_key=image["object_key"].replace("card", "second")) for image in images], "seller")
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    assert client.post("/api/v1/listings/second/publish").status_code == 200
    assert len(client.get("/api/v1/marketplace/top?limit=20").json()) == 2
    assert client.get("/api/v1/marketplace/card").json()["card_type"] == "Fire"
    ranked = client.get("/api/v1/marketplace/top?limit=20").json()
    assert [listing["id"] for listing in ranked] == ["card"]
    assert ranked[0]["card_type"] == "Fire"
    assert [item["id"] for item in client.get("/api/v1/marketplace?q=Obsidian").json()] == ["card"]


@pytest.mark.parametrize(
    "price_cents,expected",
    [(0, 1), (8_000, 1), (8_001, 2), (10_000, 2), (10_001, 3),
     (20_000, 3), (20_001, 4), (25_000, 4), (25_001, 5), (100_000, 5)],
)
def test_price_tier_boundaries(price_cents, expected):
    assert price_tier(price_cents) == expected


def test_market_and_grading_filters_preserve_shared_marketplace_features(market):
    client, db, _, images = market
    db.upsert_listing("magic-graded", {
        "market": "magic", "grading_status": "graded", "grading_company": "CGC",
        "grade": "9.5", "certification_number": "MTG-123", "title": "Black Lotus",
        "description": "Seller-reported slab label; inspect all photos.", "price_cents": 7_500,
        "currency": "USD", "card_name": "Black Lotus", "estimated_condition": "Graded 9.5",
        "status": "draft",
    }, "seller")
    db.replace_images("magic-graded", [
        dict(image, object_key=image["object_key"].replace("card", "magic-graded")) for image in images
    ], "seller")
    db.upsert_listing("sports-raw", {
        "market": "sports", "grading_status": "ungraded", "title": "Rookie Card",
        "description": "Ungraded sports card with condition disclosure.", "price_cents": 5_000,
        "currency": "USD", "card_name": "Rookie Card", "estimated_condition": "Near Mint",
        "status": "draft",
    }, "seller")
    db.replace_images("sports-raw", [
        dict(image, object_key=image["object_key"].replace("card", "sports-raw")) for image in images
    ], "seller")

    for listing_id in ("card", "magic-graded", "sports-raw"):
        assert client.post(f"/api/v1/listings/{listing_id}/publish").status_code == 200

    magic = client.get("/api/v1/marketplace?market=magic&grading_status=graded").json()
    assert [item["id"] for item in magic] == ["magic-graded"]
    assert magic[0]["grading_company"] == "CGC"
    assert magic[0]["price_tier"] == 1
    assert client.get("/api/v1/marketplace?market=magic&grading_status=ungraded").json() == []
    sports = client.get("/api/v1/marketplace?market=sports&grading_status=ungraded").json()
    assert [item["id"] for item in sports] == ["sports-raw"]
    pokemon = client.get("/api/v1/marketplace?market=pokemon&grading_status=ungraded").json()
    assert [item["id"] for item in pokemon] == ["card"]


def test_graded_listing_requires_complete_slab_label(market):
    client, db, _, _ = market
    db.upsert_listing("card", {
        "market": "sports", "grading_status": "graded", "grading_company": "PSA",
        "grade": "10", "certification_number": None,
    }, "seller")
    response = client.post("/api/v1/listings/card/publish")
    assert response.status_code == 422
    assert "certification number" in response.json()["detail"]


@pytest.mark.parametrize("field,value", [("title", "  "), ("description", ""), ("card_name", None), ("estimated_condition", ""), ("price_cents", 0), ("price_cents", 8001), ("currency", "EUR")])
def test_incomplete_or_over_limit_publish_rejected(market, field, value):
    client, db, _, _ = market
    db.upsert_listing("card", {field: value}, "seller")
    assert client.post("/api/v1/listings/card/publish").status_code == 422
    assert client.get("/api/v1/marketplace").json() == []


def test_photos_ownership_and_status_bypass(market):
    client, db, _, images = market
    assert client.put("/api/v1/listings/card", json={"status": "published"}).status_code == 422
    app.dependency_overrides[require_user] = lambda: db.get_user("other")
    assert client.post("/api/v1/listings/card/publish").status_code == 404
    assert client.post("/api/v1/listings/card/unpublish").status_code == 404
    app.dependency_overrides[require_user] = lambda: db.get_user("seller")
    db.replace_images("card", images[:3], "seller")
    assert client.post("/api/v1/listings/card/publish").status_code == 422
    db.replace_images("card", images, "seller")
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    assert client.put("/api/v1/listings/card", json={"description": " "}).status_code == 422
    assert db.get_listing("card", "seller")["description"] == "Condition disclosure"
    db.replace_images("card", images[:3], "seller")
    assert client.get("/api/v1/marketplace").json() == []
    app.dependency_overrides.pop(require_user)
    assert client.post("/api/v1/listings/card/publish").status_code == 401


def test_manual_legacy_published_flag_is_not_public(market):
    client, db, _, _ = market
    with db.sessions.begin() as session:
        listing = session.get(Listing, "card")
        listing.status = "published"
        listing.publication_approved = False
    assert client.get("/api/v1/marketplace").json() == []


def test_abandoned_checkout_does_not_reserve_and_first_payment_wins(market):
    client, db, _, _ = market
    db.create_user("third", "third@example.com", "Third", "hash", "salt", 10000)
    assert client.post("/api/v1/listings/card/publish").status_code == 200

    first = db.create_pending_order("order-1", "card", "other", 4)
    second = db.create_pending_order("order-2", "card", "third", 4)
    db.attach_checkout_session(first["id"], "cs_first")
    db.attach_checkout_session(second["id"], "cs_second")

    winner = db.claim_order_payment("cs_first", "pi_first")
    assert winner["won"] is True
    assert winner["order"]["status"] == "paid"

    loser = db.claim_order_payment("cs_second", "pi_second")
    assert loser["won"] is False
    assert loser["order"]["status"] == "refund_pending"
    refunded = db.mark_order_refunded("order-2", "re_second")
    assert refunded["status"] == "refunded"
    assert refunded["stripe_refund_id"] == "re_second"
    assert client.get("/api/v1/marketplace/card").status_code == 404


def test_stripe_webhook_accepts_raw_json_body(market, monkeypatch):
    client, db, _, _ = market
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    order = db.create_pending_order("order-webhook", "card", "other", 4)
    db.attach_checkout_session(order["id"], "cs_raw_json")

    class FakeWebhook:
        @staticmethod
        def construct_event(payload, signature, secret):
            assert isinstance(payload, bytes)
            assert b'"type":"checkout.session.completed"' in payload
            assert signature == "test-signature"
            assert secret == "whsec_test"
            return {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_raw_json",
                        "payment_status": "paid",
                        "payment_intent": "pi_raw_json",
                        "collected_information": {
                            "shipping_details": {
                                "name": "Test Buyer",
                                "address": {
                                    "line1": "123 Test Street",
                                    "line2": "Apt 4",
                                    "city": "Seattle",
                                    "state": "WA",
                                    "postal_code": "98101",
                                    "country": "US",
                                },
                            }
                        },
                    }
                },
            }

    class FakeStripe:
        Webhook = FakeWebhook

    monkeypatch.setattr(main.settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(main, "require_stripe", lambda: FakeStripe())
    response = client.post(
        "/api/v1/payments/webhook",
        json={"type": "checkout.session.completed"},
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}
    saved = db.get_order("order-webhook", "other")
    assert saved["status"] == "paid"
    assert saved["shipping_name"] == "Test Buyer"
    assert saved["shipping_line1"] == "123 Test Street"
    assert saved["listing_title"] == "Dracozolt VMAX"
    assert saved["seller_display_name"] == "Seller"


def test_order_shipping_delivery_protection_and_completion(market):
    client, db, _, _ = market
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    order = db.create_pending_order("order-lifecycle", "card", "other", 4, shipping_cents=500)
    assert order["seller_amount_cents"] == 1250 + 500 - 50
    db.attach_checkout_session(order["id"], "cs_lifecycle")
    assert db.claim_order_payment("cs_lifecycle", "pi_lifecycle")["won"] is True

    seller_orders = client.get("/api/v1/orders").json()
    assert seller_orders[0]["listing_title"] == "Dracozolt VMAX"
    assert seller_orders[0]["buyer_display_name"] == "Other"
    shipped = client.post(
        "/api/v1/orders/order-lifecycle/tracking",
        json={"tracking_number": "9400111899223856928499"},
    )
    assert shipped.status_code == 200
    assert shipped.json()["status"] == "shipped"

    app.dependency_overrides[require_user] = lambda: db.get_user("other")
    delivered = client.post("/api/v1/orders/order-lifecycle/confirm-delivery")
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "protection_hold"
    assert client.post("/api/v1/orders/order-lifecycle/complete").status_code == 409

    with db.sessions.begin() as session:
        stored = session.get(Order, "order-lifecycle")
        stored.hold_until = utc_now() - timedelta(seconds=1)
    completed = client.post("/api/v1/orders/order-lifecycle/complete")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert db.get_user("other")["completed_buys"] == 1
    assert db.get_user("seller")["successful_sales"] == 1
    assert db.get_user("seller")["seller_tier"] == 2
    assert client.post("/api/v1/orders/order-lifecycle/complete").status_code == 200
    assert db.get_user("other")["completed_buys"] == 1
    assert db.get_user("seller")["successful_sales"] == 1


def test_buyer_cannot_add_tracking(market):
    client, db, _, _ = market
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    order = db.create_pending_order("order-role", "card", "other", 4)
    db.attach_checkout_session(order["id"], "cs_role")
    db.claim_order_payment("cs_role", "pi_role")
    app.dependency_overrides[require_user] = lambda: db.get_user("other")
    response = client.post(
        "/api/v1/orders/order-role/tracking",
        json={"tracking_number": "NOT-ALLOWED"},
    )
    assert response.status_code == 404


def test_return_lifecycle_holds_payout_refunds_and_restores_draft(market):
    client, db, _, _ = market
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    order = db.create_pending_order("order-return", "card", "other", 4)
    db.attach_checkout_session(order["id"], "cs_return")
    assert db.claim_order_payment("cs_return", "pi_return")["won"] is True
    db.set_tracking(order["id"], "seller", "OUTBOUND-TRACKING")
    delivered = db.confirm_delivery(order["id"], "other", 10)
    assert delivered["status"] == "protection_hold"

    requested = db.request_return(order["id"], "other", "not_as_described", "Condition mismatch")
    assert requested["status"] == "return_requested"
    assert requested["payout_status"] == "on_hold"
    assert db.review_return(order["id"], "seller", True)["status"] == "return_approved"
    assert db.set_return_tracking(order["id"], "other", "RETURN-TRACKING")["status"] == "return_shipped"
    assert db.begin_return_refund(order["id"], "seller")["status"] == "refund_pending"
    refunded = db.mark_order_refunded(order["id"], "re_return")
    assert refunded["status"] == "refunded"
    assert refunded["payout_status"] == "not_applicable"
    assert db.get_listing("card", "seller")["status"] == "draft"


def test_ten_day_timeouts_auto_pay_seller_and_refund_buyer(market, monkeypatch):
    client, db, _, _ = market
    db.set_stripe_account("seller", "acct_auto")

    class FakeTransfer:
        calls = []

        @classmethod
        def create(cls, **kwargs):
            cls.calls.append(kwargs)
            return {"id": "tr_auto"}

    class FakeRefund:
        calls = []

        @classmethod
        def create(cls, **kwargs):
            cls.calls.append(kwargs)
            return {"id": "re_auto"}

    class FakeStripe:
        Transfer = FakeTransfer
        Refund = FakeRefund

    monkeypatch.setattr(main, "require_stripe", lambda: FakeStripe())

    assert client.post("/api/v1/listings/card/publish").status_code == 200
    purchase = db.create_pending_order("order-auto-payout", "card", "other", 4)
    db.attach_checkout_session(purchase["id"], "cs_auto_payout")
    db.claim_order_payment("cs_auto_payout", "pi_auto_payout")
    shipped = db.set_tracking(purchase["id"], "seller", "OUTBOUND-AUTO", 10)
    assert shipped["delivery_confirmation_due_at"] is not None
    with db.sessions.begin() as session:
        session.get(Order, purchase["id"]).delivery_confirmation_due_at = utc_now() - timedelta(seconds=1)

    asyncio.run(main.process_due_settlements(db))

    completed = db.get_order(purchase["id"], "other")
    assert completed["status"] == "completed"
    assert completed["payout_status"] == "paid"
    assert completed["stripe_transfer_id"] == "tr_auto"
    assert FakeTransfer.calls[0]["idempotency_key"] == "pokemarket-order-payout-order-auto-payout"

    return_listing_id = "return-timeout-card"
    db.upsert_listing(
        return_listing_id,
        dict(
            title="Return timeout card",
            description="Condition disclosure",
            price_cents=1250,
            currency="USD",
            card_name="Return card",
            estimated_condition="Near Mint",
            status="draft",
        ),
        "seller",
    )
    return_images = [
        dict(
            label="required_" + label,
            object_key=f"listings/{return_listing_id}/{label}",
            content_type="image/jpeg",
            size_bytes=100,
        )
        for label in ("front_straight", "front_slight_left", "front_slight_right", "back")
    ]
    db.replace_images(return_listing_id, return_images, "seller")
    db.set_publication(return_listing_id, "seller", True)
    returned = db.create_pending_order("order-auto-refund", return_listing_id, "other", 4)
    db.attach_checkout_session(returned["id"], "cs_auto_refund")
    db.claim_order_payment("cs_auto_refund", "pi_auto_refund")
    db.set_tracking(returned["id"], "seller", "OUTBOUND-RETURN", 10)
    requested = db.request_return(returned["id"], "other", "not_as_described", "Test timeout")
    assert requested["status"] == "return_requested"
    db.review_return(returned["id"], "seller", True)
    return_shipped = db.set_return_tracking(returned["id"], "other", "RETURN-AUTO", 10)
    assert return_shipped["return_confirmation_due_at"] is not None
    with db.sessions.begin() as session:
        session.get(Order, returned["id"]).return_confirmation_due_at = utc_now() - timedelta(seconds=1)

    asyncio.run(main.process_due_settlements(db))

    refunded = db.get_order(returned["id"], "other")
    assert refunded["status"] == "refunded"
    assert refunded["stripe_refund_id"] == "re_auto"
    assert db.get_listing(return_listing_id, "seller")["status"] == "draft"
    assert FakeRefund.calls[0]["idempotency_key"] == "pokemarket-return-order-auto-refund"


def test_admin_can_list_users_and_override_seller_tier(market):
    _, db, _, _ = market
    admin = db.set_admin("seller", True)
    assert admin["is_admin"] is True
    users = db.list_users(query="other@example")
    assert [user["id"] for user in users] == ["other"]
    updated = db.set_seller_tier("other", 4)
    assert updated["seller_tier"] == 4
    assert updated["max_listing_cents"] == 25_000


def test_admin_access_tracks_admin_emails_source_of_truth(market, monkeypatch):
    _, db, _, _ = market
    secret = "test-admin-secret-that-is-long-enough"
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=create_access_token("seller", secret, 3600),
    )
    monkeypatch.setattr(main.settings, "auth_secret", secret)

    monkeypatch.setattr(main.settings, "admin_emails", "seller@example.com")
    promoted = asyncio.run(require_user(credentials=credentials, database=db))
    assert promoted["is_admin"] is True

    monkeypatch.setattr(main.settings, "admin_emails", "")
    removed = asyncio.run(require_user(credentials=credentials, database=db))
    assert removed["is_admin"] is False


def test_scan_job_can_be_delayed_for_gemini_capacity(market):
    _, db, _, _ = market
    job = db.create_scan_job("scan-retry-job", "card", "seller")
    claimed = db.claim_next_scan_job()
    assert claimed["attempts"] == 1
    queued = db.requeue_scan_job(job["job_id"], 15, "Gemini busy")
    assert queued["status"] == "queued"
    assert queued["next_attempt_at"] is not None


def test_migration_preserves_drafts_and_requires_explicit_publish(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'migration.db'}")
    db.initialize()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE listings DROP COLUMN publication_approved")
        connection.exec_driver_sql("INSERT INTO listings (id, status, currency, photos_persisted, created_at, updated_at) VALUES ('old', 'published', 'USD', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    db.initialize()
    db.initialize()
    with db.sessions() as session:
        listing = session.get(Listing, "old")
        assert listing.status == "draft"
        assert not listing.publication_approved
    db.engine.dispose()


def test_tracking_validation_admin_sales_report_and_retention(market):
    client, db, _, _ = market
    assert classify_tracking_number("1Z 999 AA1 01 2345 6784") == ("1Z999AA10123456784", "UPS")
    with pytest.raises(TrackingValidationError):
        classify_tracking_number("made-up-tracking")

    assert client.post("/api/v1/listings/card/publish").status_code == 200
    order = db.create_pending_order("order-admin-sale", "card", "other", 4, 250)
    db.attach_checkout_session(order["id"], "cs_admin_sale")
    db.claim_order_payment("cs_admin_sale", "pi_admin_sale")
    db.save_shipping_details("cs_admin_sale", {"name": "Buyer", "address": {
        "line1": "2 Buyer Way", "city": "Kennewick", "state": "WA", "postal_code": "99336", "country": "US"
    }})
    db.set_tracking(order["id"], "seller", "1Z999AA10123456784", "UPS", 10)

    participant = db.get_order(order["id"], "other")
    assert participant["seller_shipping_line1"] is None
    sale = db.get_admin_sale(order["id"])
    assert sale["buyer_email"] == "other@example.com"
    assert sale["seller_email"] == "seller@example.com"
    assert sale["seller_shipping_line1"] == "1 Market Street"
    assert sale["tracking_carrier"] == "UPS"
    assert sale["audit_events"]
    assert b"buyer@example" not in build_sales_csv([sale])
    assert b"other@example.com" in build_sales_csv([sale])

    with db.sessions.begin() as session:
        stored = session.get(Order, order["id"])
        stored.paid_at = utc_now() - timedelta(days=31)
    assert db.redact_expired_sale_addresses(30) == 1
    redacted = db.get_admin_sale(order["id"])
    assert redacted["shipping_line1"] is None
    assert redacted["seller_shipping_line1"] is None
    assert redacted["pii_redacted_at"] is not None
    assert redacted["item_cents"] == 1250


def test_rarity_normalization_uses_tcgdex_or_ai_labels():
    assert normalize_rarity("Special Illustration Rare") == "special_illustration_rare"
    assert normalize_rarity("Hyper Rare") == "hyper_illustration_rare"
    assert rarity_from_ai_result({"tcgdex": {"rarity": "Ultra Rare"}}) == "ultra_rare"
    assert rarity_from_ai_result({"identification": {"rarity": "Double Rare"}}) == "double_rare"


def test_tracking_number_cannot_be_reused(market):
    client, db, _, images = market
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    first = db.create_pending_order("reuse-one", "card", "other", 4)
    db.attach_checkout_session(first["id"], "cs_reuse_one"); db.claim_order_payment("cs_reuse_one", "pi_reuse_one")
    db.set_tracking(first["id"], "seller", "1Z999AA10123456784", "UPS")

    db.upsert_listing("card-two", dict(title="Second", description="Disclosure", price_cents=1000, currency="USD", card_name="Second", estimated_condition="NM", status="draft"), "seller")
    db.replace_images("card-two", [{**image, "object_key": image["object_key"].replace("card/", "card-two/")} for image in images], "seller")
    db.set_publication("card-two", "seller", True)
    second = db.create_pending_order("reuse-two", "card-two", "other", 4)
    db.attach_checkout_session(second["id"], "cs_reuse_two"); db.claim_order_payment("cs_reuse_two", "pi_reuse_two")
    with pytest.raises(Exception, match="already attached"):
        db.set_tracking(second["id"], "seller", "1Z999AA10123456784", "UPS")
