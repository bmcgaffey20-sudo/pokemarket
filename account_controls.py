"""Account restrictions, deletion outbox, and PII-minimized financial ledger.

Uses the existing database. No live credentials or external writes at import time.
"""
import asyncio
import csv
import io
import re
import secrets
from copy import copy
from datetime import timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Text, DateTime, Integer, JSON, select, or_, delete

from database import Base, User, Order, Listing, ListingImage, ListingView, ScanJob, DeviceToken, AccountAction, ListingValidationError, utc_now
from auth import verify_password
from recovery import limit_auth


class AccountControl(Base):
    __tablename__ = "account_controls"
    user_id = Column(String(36), primary_key=True)
    state = Column(String(24), nullable=False, default="active")
    suspended_until = Column(DateTime(timezone=True))
    deletion_requested_at = Column(DateTime(timezone=True))
    deleted_at = Column(DateTime(timezone=True))
    reason = Column(String(500))


class AccountAudit(Base):
    __tablename__ = "account_control_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), nullable=False, index=True)
    actor_id = Column(String(36), nullable=False)
    action = Column(String(32), nullable=False)
    reason = Column(String(500), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class RetainedTransaction(Base):
    __tablename__ = "retained_transactions"
    # Deliberately no cascading foreign keys, addresses, emails, names or messages.
    order_id = Column(String(36), primary_key=True)
    transaction_at = Column(DateTime(timezone=True), nullable=False, index=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class DeletionObject(Base):
    __tablename__ = "account_deletion_objects"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), nullable=False, index=True)
    bucket_kind = Column(String(16), nullable=False)
    object_key = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime(timezone=True))


FINANCIAL_FIELDS = (
    "id", "listing_id", "buyer_id", "seller_id", "status", "currency", "item_cents",
    "shipping_cents", "commission_cents", "seller_amount_cents", "payout_status",
    "stripe_payment_intent_id", "stripe_transfer_id", "stripe_refund_id",
)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def control_state(row, now=None):
    if not row:
        return "active"
    if row.deleted_at:
        return "deleted"
    if row.deletion_requested_at:
        return "deletion_pending"
    if row.state == "suspended" and row.suspended_until and aware(row.suspended_until) <= (now or utc_now()):
        return "active"
    return row.state


def status(db, user_id):
    with db.sessions() as session:
        row = session.get(AccountControl, user_id)
        pending = session.query(DeletionObject).filter_by(user_id=user_id).count()
        return {"state": control_state(row), "suspended_until": row.suspended_until if row else None,
                "reason": row.reason if row else None, "cloud_cleanup_pending": pending}


def enforce(db, user_id, request):
    state = status(db, user_id)["state"]
    if state == "deleted":
        raise HTTPException(401, "This account has been deleted.")
    if state == "active":
        return
    path = request.url.path
    # Restricted accounts keep access to existing orders and deletion, never new commerce.
    allowed = path in {"/api/v1/auth/me", "/api/v1/account/status", "/api/v1/account/delete"}
    allowed |= path == "/api/v1/account/seller-address"
    allowed |= path == "/api/v1/payments/connect/onboard"
    allowed |= request.method == "GET" and (path == "/api/v1/orders" or path.startswith("/api/v1/orders/"))
    allowed |= request.method == "POST" and bool(re.fullmatch(r"/api/v1/orders/[^/]+/(tracking|confirm-delivery|complete|returns|returns/review|returns/tracking|returns/received)", path))
    if not allowed:
        raise HTTPException(403, f"Account {state.replace('_', ' ')}. Existing orders and account deletion remain available. Contact support to appeal.")


def require_active_transaction(session, user_id):
    # Serialize new commerce against deletion/moderation using the existing user row.
    user = session.get(User, user_id, with_for_update=True)
    if user is None or control_state(session.get(AccountControl, user_id)) != "active":
        raise ListingValidationError("Account is restricted; new marketplace activity is unavailable.")


def snapshot(session, order):
    if not order.paid_at and order.status in {"pending_payment", "canceled"}:
        return
    row = session.get(RetainedTransaction, order.id)
    if row is None:
        row = RetainedTransaction(order_id=order.id)
        session.add(row)
    row.transaction_at = order.paid_at or order.created_at
    row.data = {field: getattr(order, field) for field in FINANCIAL_FIELDS}
    row.data["paid_at"] = aware(row.transaction_at).isoformat()
    row.updated_at = utc_now()


def sync_ledger(db):
    # Bounded batches avoid loading the entire financial history into Render RAM.
    cursor = ""
    while True:
        with db.sessions.begin() as session:
            orders = session.scalars(select(Order).outerjoin(RetainedTransaction, RetainedTransaction.order_id == Order.id).where(
                Order.id > cursor,
                or_(Order.paid_at.is_not(None), Order.status.not_in(("pending_payment", "canceled"))),
                or_(RetainedTransaction.order_id.is_(None), Order.updated_at > RetainedTransaction.updated_at)
            ).order_by(Order.id).limit(200)).all()
            for order in orders:
                snapshot(session, order)
            if not orders:
                break
            cursor = orders[-1].id


def ledger_rows(db, start, end):
    sync_ledger(db)
    with db.sessions() as session:
        return [dict(row.data) for row in session.scalars(select(RetainedTransaction).where(
            RetainedTransaction.transaction_at >= start, RetainedTransaction.transaction_at < end
        ).order_by(RetainedTransaction.transaction_at, RetainedTransaction.order_id))]


def unresolved(order):
    if order.status == "canceled":
        return False
    if order.status == "refunded" and order.stripe_refund_id:
        return False
    if order.status == "completed" and order.payout_status in {"paid", "transferred"}:
        return False
    return True


def withdraw(session, user_id):
    for listing in session.scalars(select(Listing).where(Listing.seller_id == user_id, Listing.status == "published")):
        listing.status = "draft"
        listing.publication_approved = False


def purge_account(db, user_id):
    from community import Conversation, Message, Attachment, Feedback, UserBlock
    from alerts import Notification, MessageRead
    with db.sessions.begin() as session:
        user = session.get(User, user_id, with_for_update=True)
        control = session.get(AccountControl, user_id, with_for_update=True)
        if not user or not control or not control.deletion_requested_at or control.deleted_at:
            return False
        orders = session.scalars(select(Order).where(or_(Order.buyer_id == user_id, Order.seller_id == user_id))).all()
        if any(unresolved(order) for order in orders):
            return False
        # Do not purge while an in-flight scan can still write data or photo objects.
        if session.scalar(select(ScanJob.id).where(ScanJob.user_id == user_id, ScanJob.status == "processing").limit(1)):
            return False
        for order in orders:
            snapshot(session, order)
            for prefix in ("shipping_", "seller_shipping_"):
                for field in ("name", "line1", "line2", "city", "state", "postal_code", "country"):
                    setattr(order, prefix + field, None)
            order.return_notes = None
            order.tracking_number = order.return_tracking_number = None
            order.pii_redacted_at = utc_now()
        listings = session.scalars(select(Listing).where(Listing.seller_id == user_id)).all()
        for listing in listings:
            # Sweep unfinished uploads too, after all previously signed PUT links expire.
            session.add(DeletionObject(user_id=user_id, bucket_kind="listing_prefix", object_key=f"listings/{listing.id}/originals/"))
            for img in list(listing.images):
                session.add(DeletionObject(user_id=user_id, bucket_kind="listing", object_key=img.object_key))
                session.delete(img)
            session.execute(delete(ScanJob).where(ScanJob.listing_id == listing.id))
            session.execute(delete(ListingView).where(ListingView.listing_id == listing.id))
            from cart import CollectionItem
            session.execute(delete(CollectionItem).where(CollectionItem.listing_id == listing.id))
            listing.status = "archived"
            listing.publication_approved = False
            listing.title = "Deleted listing"
            listing.description = listing.ai_result = None
            listing.card_name = listing.set_name = listing.card_number = listing.certification_number = None
            listing.photos_persisted = False
        chats = session.scalars(select(Conversation).where(or_(Conversation.buyer_id == user_id, Conversation.seller_id == user_id))).all()
        for chat in chats:
            for attachment in session.scalars(select(Attachment).where(Attachment.conversation_id == chat.id)):
                session.add(DeletionObject(user_id=user_id, bucket_kind="message", object_key=attachment.object_key))
                if attachment.object_key.startswith("message-photos/"):
                    session.add(DeletionObject(user_id=user_id, bucket_kind="message", object_key=attachment.object_key.replace("message-photos/", "messages/", 1)))
                session.delete(attachment)
            session.execute(delete(Message).where(Message.conversation_id == chat.id))
            session.execute(delete(MessageRead).where(MessageRead.conversation_id == chat.id))
            session.delete(chat)
        session.execute(delete(Feedback).where(or_(Feedback.author_id == user_id, Feedback.recipient_id == user_id)))
        session.execute(delete(UserBlock).where(or_(UserBlock.owner_id == user_id, UserBlock.blocked_id == user_id)))
        for model in (Notification, DeviceToken, AccountAction, ScanJob):
            session.execute(delete(model).where(model.user_id == user_id))
        from cart import CollectionItem
        from community import HiddenMessage
        session.execute(delete(CollectionItem).where(CollectionItem.user_id == user_id))
        session.execute(delete(HiddenMessage).where(HiddenMessage.user_id == user_id))
        user.email = f"deleted-{user.id}@deleted.invalid"
        user.display_name = "Deleted account"
        user.password_hash = user.password_salt = "disabled"
        user.stripe_account_id = None
        user.email_verified = user.is_admin = False
        user.last_login_at = None
        user.session_version += 1
        for field in User.__table__.columns.keys():
            if field.startswith("seller_address_"):
                setattr(user, field, None)
        control.state, control.deleted_at, control.reason = "deleted", utc_now(), None
        # Free-text moderation notes may contain personal data; retain event metadata only.
        for audit in session.scalars(select(AccountAudit).where(AccountAudit.user_id == user_id)):
            audit.reason = "Redacted following account deletion"
        session.add(AccountAudit(user_id=user_id, actor_id=user_id, action="deleted", reason="User requested deletion"))
    return True


def process_deletions(db, settings, storage_factory):
    with db.sessions() as session:
        ids = list(session.scalars(select(AccountControl.user_id).where(AccountControl.deletion_requested_at.is_not(None), AccountControl.deleted_at.is_(None))))
    for uid in ids:
        purge_account(db, uid)
    with db.sessions() as session:
        jobs = list(session.scalars(select(DeletionObject).where(or_(DeletionObject.last_attempt_at.is_(None), DeletionObject.last_attempt_at < utc_now()-timedelta(hours=1))).limit(100)))
    for job in jobs:
        try:
            with db.sessions() as session:
                owner = session.get(AccountControl, job.user_id)
                if owner and owner.deleted_at and aware(owner.deleted_at) + timedelta(seconds=max(3600, getattr(settings, "r2_presigned_url_expiry_seconds", 3600)) + 300) > utc_now():
                    continue
            storage = copy(storage_factory())
            if job.bucket_kind == "message":
                if not settings.r2_message_bucket_name:
                    raise RuntimeError("Private message bucket is not configured")
                storage.bucket_name = settings.r2_message_bucket_name
            if job.bucket_kind == "listing_prefix":
                storage.delete_listing_prefix(job.object_key)
            else:
                storage.delete_objects([job.object_key])
        except Exception:
            with db.sessions.begin() as session:
                row = session.get(DeletionObject, job.id)
                if row:
                    row.attempts += 1
                    row.last_attempt_at = utc_now()
        else:
            with db.sessions.begin() as session:
                session.execute(delete(DeletionObject).where(DeletionObject.id == job.id))


class ModerationInput(BaseModel):
    action: Literal["suspend_7d", "suspend_30d", "ban", "restore"]
    reason: str = Field(min_length=3, max_length=500)


class DeleteInput(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    confirmation: Literal["DELETE"]


def install_account_controls(app, settings, require_database, require_user, require_admin):
    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return Path(__file__).with_name("privacy.html").read_text(encoding="utf-8")

    @app.get("/account-deletion", response_class=HTMLResponse)
    def deletion_page():
        nonce = secrets.token_urlsafe(24)
        page = Path(__file__).with_name("account_deletion.html").read_text(encoding="utf-8").replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})

    @app.get("/api/v1/account/status")
    def account_status(user=Depends(require_user), db=Depends(require_database)):
        return status(db, user["id"])

    @app.get("/api/v1/admin/users/{user_id}/account-control")
    def admin_status(user_id: str, admin=Depends(require_admin), db=Depends(require_database)):
        with db.sessions() as session:
            if not session.get(User, user_id):
                raise HTTPException(404, "User not found")
            history = session.scalars(select(AccountAudit).where(AccountAudit.user_id == user_id).order_by(AccountAudit.id.desc()).limit(50)).all()
            return {**status(db, user_id), "audit": [{"action": x.action, "actor_id": x.actor_id, "reason": x.reason, "created_at": x.created_at} for x in history]}

    @app.post("/api/v1/admin/users/{user_id}/account-control")
    def moderate(user_id: str, payload: ModerationInput, admin=Depends(require_admin), db=Depends(require_database)):
        if not payload.reason.strip():
            raise HTTPException(422, "A reason is required")
        with db.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user:
                raise HTTPException(404, "User not found")
            if user_id == admin["id"] or user.is_admin or user.email.lower() in settings.admin_email_list:
                raise HTTPException(409, "Administrator accounts cannot be restricted here.")
            row = session.get(AccountControl, user_id)
            if row is None:
                row = AccountControl(user_id=user_id)
                session.add(row)
            if row.deleted_at or row.deletion_requested_at:
                raise HTTPException(409, "Account deletion cannot be reversed by moderation.")
            row.state = "active" if payload.action == "restore" else "banned" if payload.action == "ban" else "suspended"
            days = 7 if payload.action == "suspend_7d" else 30
            row.suspended_until = utc_now()+timedelta(days=days) if row.state == "suspended" else None
            row.reason = payload.reason.strip()
            if row.state != "active":
                withdraw(session, user_id)
            session.add(AccountAudit(user_id=user_id, actor_id=admin["id"], action=payload.action, reason=row.reason))
        return status(db, user_id)

    @app.post("/api/v1/account/delete")
    async def request_deletion(payload: DeleteInput, request: Request, user=Depends(require_user), db=Depends(require_database)):
        await limit_auth(db, request, "account-delete", user["id"], 5)
        credentials = await asyncio.to_thread(db.get_user_by_email, user["email"])
        if not credentials or not await asyncio.to_thread(verify_password, payload.password, credentials["password_salt"], credentials["password_hash"], credentials["password_iterations"]):
            raise HTTPException(401, "Password is incorrect")
        with db.sessions.begin() as session:
            owner = session.get(User, user["id"], with_for_update=True)
            if owner.is_admin or owner.email.lower() in settings.admin_email_list:
                raise HTTPException(409, "Remove this account from ADMIN_EMAILS and transfer administration before deletion.")
            row = session.get(AccountControl, owner.id)
            if row is None:
                row = AccountControl(user_id=owner.id, state="active")
                session.add(row)
            if not row.deletion_requested_at:
                row.deletion_requested_at = utc_now()
                session.add(AccountAudit(user_id=owner.id, actor_id=owner.id, action="deletion_requested", reason="User requested deletion"))
            withdraw(session, owner.id)
            for job in session.scalars(select(ScanJob).where(ScanJob.user_id == owner.id, ScanJob.status == "queued")):
                job.status, job.result, job.error = "failed", None, "Account deletion requested"
        deleted = await asyncio.to_thread(purge_account, db, user["id"])
        return {"state": "deleted" if deleted else "deletion_pending", "message": "Account deleted. Cloud-file cleanup is queued." if deleted else "Deletion requested. Existing orders, payouts, refunds or active scans must finish before final removal. No new marketplace activity is allowed."}

    @app.get("/api/v1/admin/retained-transactions")
    def retained(offset: int = 0, admin=Depends(require_admin), db=Depends(require_database)):
        if offset < 0:
            raise HTTPException(422, "Invalid offset")
        sync_ledger(db)
        with db.sessions() as session:
            rows = session.scalars(select(RetainedTransaction).order_by(RetainedTransaction.transaction_at.desc(), RetainedTransaction.order_id).offset(offset).limit(100)).all()
            return [dict(row.data) for row in rows]
