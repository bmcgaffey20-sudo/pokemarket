from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
import main
from database import Database, Listing, Order
from community import Message, Attachment
from community import membership_age
from datetime import date


@pytest.fixture
def community(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'community.db'}")
    db.initialize()
    users = {}
    for name in ("seller", "buyer", "stranger"):
        users[name], _ = db.create_user(name, f"{name}@example.com", name, "hash", "salt", 10000)
    with db.sessions.begin() as session:
        session.add(Listing(id="listing", seller_id="seller", title="Card", status="published", price_cents=100))
        session.add(Order(id="order", listing_id="listing", buyer_id="buyer", seller_id="seller", status="completed",
                          item_cents=100, commission_cents=10, seller_amount_cents=90))
    actor = {"name": "buyer"}
    main.app.dependency_overrides[main.require_database] = lambda: db
    main.app.dependency_overrides[main.require_user] = lambda: users[actor["name"]]
    monkeypatch.setattr(main.settings, "firebase_service_account_json", None)
    monkeypatch.setattr(main.settings, "r2_message_bucket_name", "private-chat")
    class Storage:
        bucket_name = "listing-bucket"
        url_expiry = 600
        objects = {}
        @property
        def client(self): return self
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            assert Params["Bucket"] == "private-chat"
            return "https://private.example/" + Params["Key"]
        def get_object(self, Bucket, Key): return {"Body": BytesIO(self.objects[Key])}
        def put_object(self, Bucket, Key, Body, **kwargs): self.objects[Key] = Body
        def delete_objects(self, keys):
            for key in keys: self.objects.pop(key, None)
        def presign_object(self, key): return "https://private.example/" + key
    storage = Storage()
    monkeypatch.setattr(main, "get_r2_storage", lambda: storage)
    yield TestClient(main.app), db, actor, storage
    main.app.dependency_overrides.clear()
    db.engine.dispose()


def start(client):
    response = client.post("/api/v1/listings/listing/conversation")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_conversation_identity_privacy_and_idempotent_send(community):
    client, db, actor, _ = community
    chat = start(client)
    assert start(client) == chat
    payload = {"body": "Can I see the corners?", "client_id": str(uuid4())}
    first = client.post(f"/api/v1/messages/{chat}", json=payload)
    assert first.status_code == 200
    assert client.post(f"/api/v1/messages/{chat}", json=payload).json() == first.json()
    assert len(client.get(f"/api/v1/messages/{chat}").json()["messages"]) == 1
    assert client.get(f"/api/v1/messages/{chat}").headers["cache-control"] == "no-store"
    actor["name"] = "stranger"
    assert client.get("/api/v1/messages").json() == []
    assert client.get(f"/api/v1/messages/{chat}").status_code == 404
    assert client.post(f"/api/v1/messages/{chat}", json=payload).status_code == 404
    assert client.post(f"/api/v1/messages/{chat}/attachments", json={"content_type": "image/jpeg", "size_bytes": 10}).status_code == 404
    actor["name"] = "seller"
    assert client.get(f"/api/v1/messages/{chat}").json()["messages"][0]["mine"] is False
    assert client.post("/api/v1/listings/listing/conversation").status_code == 400


def test_block_and_unblock(community):
    client, _, actor, _ = community
    chat = start(client)
    assert client.post(f"/api/v1/messages/{chat}/block").status_code == 200
    actor["name"] = "seller"
    assert client.post(f"/api/v1/messages/{chat}", json={"client_id": "one", "body": "Hi"}).status_code == 403
    client.delete(f"/api/v1/messages/{chat}/block")
    assert client.get(f"/api/v1/messages/{chat}").json()["blocked"] is True
    actor["name"] = "buyer"
    client.delete(f"/api/v1/messages/{chat}/block")
    assert client.post(f"/api/v1/messages/{chat}", json={"client_id": "one", "body": "Hi"}).status_code == 200


def test_feedback_participants_eligibility_duplicate_and_public_privacy(community):
    client, db, actor, _ = community
    actor["name"] = "stranger"
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 5}).status_code == 404
    assert client.get("/api/v1/orders/order/feedback").status_code == 404
    actor["name"] = "buyer"
    with db.sessions.begin() as session: session.get(Order, "order").status = "pending_payment"
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 5}).status_code == 409
    with db.sessions.begin() as session: session.get(Order, "order").status = "refunded"
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 6}).status_code == 422
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 4, "comment": "Helpful seller"}).status_code == 200
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 1}).status_code == 409
    public = client.get("/api/v1/listings/listing/seller-feedback").json()
    assert public["count"] == 1 and public["average"] == 4
    assert set(public["reviews"][0]) == {"rating", "comment", "recipient_role", "created_at"}
    actor["name"] = "seller"
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 5}).status_code == 200
    assert len(client.get("/api/v1/orders/order/feedback").json()["reviews"]) == 2
    assert client.get("/api/v1/account/feedback").json()["reviews"][0]["recipient_role"] == "seller"


def test_image_verification_ownership_and_private_delivery(community):
    client, db, actor, storage = community
    chat = start(client)
    image = BytesIO()
    Image.new("RGB", (32, 32)).save(image, "JPEG")
    raw = image.getvalue()
    target = client.post(f"/api/v1/messages/{chat}/attachments", json={"content_type": "image/jpeg", "size_bytes": len(raw)}).json()
    with db.sessions() as session: key = session.get(Attachment, target["id"]).object_key
    storage.objects[key] = raw
    actor["name"] = "seller"
    payload = {"client_id": "photo", "attachment_id": target["id"]}
    assert client.post(f"/api/v1/messages/{chat}", json=payload).status_code == 400
    actor["name"] = "buyer"
    assert client.post(f"/api/v1/messages/{chat}", json=payload).status_code == 200
    assert key not in storage.objects
    sent = client.get(f"/api/v1/messages/{chat}").json()["messages"][0]
    assert "/message-photos/" in sent["image_url"]
    assert client.post(f"/api/v1/messages/{chat}", json={**payload, "client_id": "reuse"}).status_code == 400


def test_reject_invalid_and_oversized_photos(community):
    client, db, _, storage = community
    chat = start(client)
    path = f"/api/v1/messages/{chat}/attachments"
    assert client.post(path, json={"content_type": "text/html", "size_bytes": 10}).status_code == 400
    assert client.post(path, json={"content_type": "image/jpeg", "size_bytes": 21 * 1024 * 1024}).status_code == 422
    target = client.post(path, json={"content_type": "image/jpeg", "size_bytes": 5}).json()
    with db.sessions() as session: key = session.get(Attachment, target["id"]).object_key
    storage.objects[key] = b"hello"
    assert client.post(f"/api/v1/messages/{chat}", json={"client_id": "bad", "attachment_id": target["id"]}).status_code == 400
    with db.sessions() as session: assert session.scalar(select(Message)) is None


def test_message_pagination_and_empty_validation(community):
    client, _, _, _ = community
    chat = start(client)
    assert client.post(f"/api/v1/messages/{chat}", json={"client_id": "empty", "body": "  "}).status_code == 400
    for i in range(3):
        assert client.post(f"/api/v1/messages/{chat}", json={"client_id": str(i), "body": str(i)}).status_code == 200
    messages = client.get(f"/api/v1/messages/{chat}").json()["messages"]
    earlier = client.get(f"/api/v1/messages/{chat}?before={messages[-1]['id']}").json()["messages"]
    assert [m["body"] for m in earlier] == ["0", "1"]


def test_profile_counts_privacy_and_membership(community):
    client, db, actor, _ = community
    p = client.get("/api/v1/users/seller/profile").json()
    assert p["sells_without_incident"] == 1
    assert p["buys_without_incident"] == 0
    assert p["seller_rating"] is None
    assert not {"email", "shipping_name", "password_hash", "stripe_account_id"} & p.keys()
    assert client.get("/api/v1/users/missing/profile").status_code == 404
    assert client.post("/api/v1/orders/order/feedback", json={"rating": 4}).status_code == 200
    assert client.get("/api/v1/users/seller/profile").json()["seller_rating"] == 4
    with db.sessions.begin() as session:
        session.get(Order, "order").return_reason = "not_as_described"
    assert client.get("/api/v1/users/seller/profile").json()["sells_without_incident"] == 0
    assert client.get("/api/v1/users/buyer/profile").json()["buys_without_incident"] == 0
    assert membership_age(date(2024, 2, 29), date(2025, 3, 1)) == {"years": 1, "months": 0, "days": 1}
    assert membership_age(date(2025, 1, 31), date(2025, 2, 28)) == {"years": 0, "months": 1, "days": 0}
