import pytest
import main
from datetime import timedelta
from fastapi.testclient import TestClient
from database import Database, Listing, Order, utc_now
from main import app, require_database, require_user


@pytest.fixture
def market(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'market.db'}")
    database.initialize()
    user, _ = database.create_user("seller", "seller@example.com", "Seller", "hash", "salt", 10000)
    database.create_user("other", "other@example.com", "Other", "hash", "salt", 10000)
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
    public = client.get("/api/v1/marketplace/card").json()
    assert public["title"] == "Dracozolt VMAX"
    assert public["seller"] == {"display_name": "Seller", "tier": 1}
    assert not {"seller_id", "scan_id", "ai_result", "email", "password_hash"} & public.keys()
    assert set(public["images"][0]) == {"label", "url"}
    assert len(client.get("/api/v1/marketplace?q=dracozolt").json()) == 1
    assert client.get("/api/v1/marketplace?q=missing").json() == []
    assert client.get("/api/v1/marketplace?offset=1").json() == []
    assert client.get("/api/v1/marketplace?limit=1000").status_code == 400
    assert client.post("/api/v1/listings/card/unpublish").status_code == 200
    assert client.get("/api/v1/marketplace/card").status_code == 404
    assert len(db.get_listing("card", "seller")["images"]) == 4


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
