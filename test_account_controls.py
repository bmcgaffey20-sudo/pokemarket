from datetime import timedelta, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from auth import hash_password, create_access_token
from database import Database, User, Listing, Order, ListingImage, utc_now, ListingValidationError
from community import Conversation, Message, Attachment
from account_controls import AccountControl, AccountAudit, RetainedTransaction, DeletionObject, purge_account, process_deletions, ledger_rows
from reporting import completed_month, build_retained_csv


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'controls.db'}")
    db.initialize()
    secret = "account-controls-tests-secret-at-least-32"
    monkeypatch.setattr(main.settings, "auth_secret", secret)
    monkeypatch.setattr(main.settings, "admin_emails", "admin@example.com")
    for uid in ("admin", "seller", "buyer"):
        salt, hashed, rounds = hash_password("test-password", 10000)
        db.create_user(uid, uid+"@example.com", uid, hashed, salt, rounds)
    with db.sessions.begin() as session:
        session.add(Listing(id="card", seller_id="seller", title="card", status="published", publication_approved=True, price_cents=100))
    main.app.dependency_overrides[main.require_database] = lambda: db
    client = TestClient(main.app)
    def headers(uid):
        return {"Authorization": "Bearer "+create_access_token(uid, secret, 3600)}
    yield client, db, headers
    main.app.dependency_overrides.clear()
    db.engine.dispose()


def test_restrictions_apply_to_existing_tokens_and_expire(accounts):
    client, db, headers = accounts
    route = "/api/v1/admin/users/seller/account-control"
    assert client.post(route, headers=headers("buyer"), json={"action":"ban","reason":"abuse"}).status_code == 403
    response = client.post(route, headers=headers("admin"), json={"action":"suspend_7d","reason":"policy violation"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "suspended"
    assert client.get("/api/v1/listings", headers=headers("seller")).status_code == 403
    assert client.get("/api/v1/orders", headers=headers("seller")).status_code == 200
    assert client.post("/api/v1/orders/missing/returns", headers=headers("seller"), json={"reason":"not_as_described","notes":"problem"}).status_code != 403
    assert client.get("/api/v1/marketplace").json() == []
    with pytest.raises(ListingValidationError):
        db.set_publication("card", "seller", True)
    with db.sessions.begin() as session:
        session.get(AccountControl,"seller").suspended_until = utc_now()-timedelta(seconds=1)
    assert client.get("/api/v1/account/status", headers=headers("seller")).json()["state"] == "active"
    for action in ("suspend_30d", "ban", "restore"):
        result = client.post(route, headers=headers("admin"), json={"action":action,"reason":"reviewed by admin"})
        assert result.status_code == 200
    assert result.json()["state"] == "active"
    assert client.post("/api/v1/admin/users/admin/account-control", headers=headers("admin"), json={"action":"ban","reason":"self"}).status_code == 409
    assert len(client.get(route,headers=headers("admin")).json()["audit"]) == 4


def add_order(db, status="completed", payout="paid"):
    with db.sessions.begin() as session:
        session.add(Order(id="order", listing_id="card", buyer_id="buyer", seller_id="seller", status=status, payout_status=payout,
            item_cents=100, shipping_cents=20, commission_cents=4, seller_amount_cents=116,
            paid_at=utc_now()-timedelta(days=2), completed_at=utc_now(), shipping_name="Private Name", shipping_line1="Private Address", stripe_transfer_id="tr_test"))


def test_deletion_preserves_finance_and_retries_cloud_cleanup(accounts):
    client, db, headers = accounts
    add_order(db)
    with db.sessions.begin() as session:
        session.add(ListingImage(listing_id="card", label="front",object_key="listings/card/originals/front.jpg",content_type="image/jpeg",size_bytes=10,sort_order=0))
        session.add(Conversation(id="chat",listing_id="card",title="card",buyer_id="buyer",seller_id="seller"))
        session.add(Message(conversation_id="chat",sender_id="seller",client_id="x",body="private message"))
        session.add(Attachment(id="attachment",conversation_id="chat",sender_id="seller",object_key="message-photos/secret.jpg",content_type="image/jpeg",size_bytes=10))
    payload={"password":"test-password","confirmation":"DELETE"}
    assert client.post("/api/v1/account/delete",headers=headers("seller"),json={**payload,"password":"wrong"}).status_code == 401
    result=client.post("/api/v1/account/delete",headers=headers("seller"),json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "deleted"
    assert client.get("/api/v1/auth/me",headers=headers("seller")).status_code == 401
    assert client.get("/api/v1/users/seller/profile").status_code == 404
    with db.sessions() as session:
        assert session.get(User,"seller").display_name == "Deleted account"
        assert session.get(Order,"order").shipping_line1 is None
        row=session.get(RetainedTransaction,"order")
        assert row.data["seller_amount_cents"] == 116
        assert "Private" not in str(row.data)
        assert list(session.scalars(select(Message))) == []
        assert len(list(session.scalars(select(DeletionObject)))) == 4
    with db.sessions.begin() as session:
        session.get(AccountControl,"seller").deleted_at = utc_now()-timedelta(hours=2)
    class Storage:
        bucket_name="public"
        calls=[]
        fail=True
        def delete_objects(self,keys):
            if self.fail: raise RuntimeError("temporary outage")
            self.calls.append((self.bucket_name,keys))
        def delete_listing_prefix(self,prefix):
            self.delete_objects([prefix])
    storage=Storage(); settings=SimpleNamespace(r2_message_bucket_name="private")
    process_deletions(db,settings,lambda:storage)
    with db.sessions.begin() as session:
        for job in session.scalars(select(DeletionObject)):
            assert job.attempts == 1
            job.last_attempt_at=utc_now()-timedelta(hours=2)
    storage.fail=False
    process_deletions(db,settings,lambda:storage)
    assert {call[0] for call in storage.calls} == {"public","private"}
    with db.sessions() as session: assert list(session.scalars(select(DeletionObject))) == []
    assert client.get("/api/v1/admin/retained-transactions",headers=headers("buyer")).status_code==403
    assert len(client.get("/api/v1/admin/retained-transactions",headers=headers("admin")).json()) == 1


def test_deletion_waits_for_orders_and_failed_payout(accounts):
    client, db, headers=accounts
    add_order(db,status="shipped",payout="pending")
    response=client.post("/api/v1/account/delete",headers=headers("seller"),json={"password":"test-password","confirmation":"DELETE"})
    assert response.json()["state"] == "deletion_pending"
    assert client.get("/api/v1/orders",headers=headers("seller")).status_code==200
    with db.sessions.begin() as session:
        order=session.get(Order,"order");order.status="completed";order.payout_status="failed"
    assert not purge_account(db,"seller")
    with db.sessions.begin() as session: session.get(Order,"order").payout_status="paid"
    assert purge_account(db,"seller")
    assert not purge_account(db,"seller")


def test_reports_and_privacy(accounts):
    client,db,headers=accounts
    add_order(db)
    rows=ledger_rows(db,utc_now()-timedelta(days=3),utc_now())
    csv=build_retained_csv(rows).decode()
    assert "Private" not in csv and "email" not in csv and "address" not in csv
    assert "116" in csv
    with db.sessions.begin() as session:
        row=session.get(Order,"order"); row.status="refunded"; row.stripe_refund_id="re_after_report"
    refreshed=ledger_rows(db,utc_now()-timedelta(days=3),utc_now())
    assert len(refreshed)==1 and refreshed[0]["stripe_refund_id"]=="re_after_report"
    start,end=completed_month(datetime(2026,1,5,tzinfo=timezone.utc))
    assert start.isoformat().startswith("2025-12-01") and end.isoformat().startswith("2026-01-01")
    assert client.get("/privacy").status_code==200
    assert "Slab Grade" in client.get("/account-deletion").text


def test_address_retention_does_not_strip_unresolved_orders(accounts):
    _,db,_=accounts
    add_order(db,status="return_disputed",payout="on_hold")
    with db.sessions.begin() as session:
        o=session.get(Order,"order");o.created_at=o.paid_at=o.updated_at=utc_now()-timedelta(days=90)
    assert db.redact_expired_sale_addresses(30)==0
