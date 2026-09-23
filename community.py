"""Participant-only listing conversations and verified transaction feedback."""
from datetime import timedelta, timezone
from uuid import uuid4
from io import BytesIO
from copy import copy

from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Integer, Text, DateTime, Boolean, UniqueConstraint, select, or_, and_, func
from sqlalchemy.exc import IntegrityError
from PIL import Image
from database import Base, Listing, Order, User, utc_now
from notifications import send_push


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("listing_id", "buyer_id", name="uq_listing_conversation"),)
    id = Column(String(36), primary_key=True)
    listing_id = Column(String(64), nullable=False)
    title = Column(String(300), nullable=False)
    buyer_id = Column(String(36), nullable=False, index=True)
    seller_id = Column(String(36), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class Message(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "sender_id", "client_id", name="uq_message_retry"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(36), nullable=False, index=True)
    sender_id = Column(String(36), nullable=False)
    client_id = Column(String(36), nullable=False)
    body = Column(Text, nullable=False)
    attachment_id = Column(String(36))
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class Attachment(Base):
    __tablename__ = "message_attachments"
    id = Column(String(36), primary_key=True)
    conversation_id = Column(String(36), nullable=False, index=True)
    sender_id = Column(String(36), nullable=False)
    object_key = Column(String(500), nullable=False)
    content_type = Column(String(32), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class UserBlock(Base):
    __tablename__ = "message_blocks"
    owner_id = Column(String(36), primary_key=True)
    blocked_id = Column(String(36), primary_key=True)


class Feedback(Base):
    __tablename__ = "transaction_feedback"
    __table_args__ = (UniqueConstraint("order_id", "author_id", name="uq_transaction_feedback"),)
    id = Column(String(36), primary_key=True)
    order_id = Column(String(36), nullable=False)
    author_id = Column(String(36), nullable=False)
    recipient_id = Column(String(36), nullable=False, index=True)
    recipient_role = Column(String(10), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(String(1000), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class MessageInput(BaseModel):
    client_id: str = Field(min_length=1, max_length=36)
    body: str = Field(default="", max_length=2000)
    attachment_id: str | None = Field(default=None, max_length=36)


class AttachmentInput(BaseModel):
    content_type: str
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)


class FeedbackInput(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


def participant(session, conversation_id, user_id):
    chat = session.get(Conversation, conversation_id)
    if chat is None or user_id not in (chat.buyer_id, chat.seller_id):
        raise HTTPException(404, "Conversation not found.")
    return chat


def blocked(session, chat):
    return session.scalar(select(UserBlock).where(or_(
        and_(UserBlock.owner_id == chat.buyer_id, UserBlock.blocked_id == chat.seller_id),
        and_(UserBlock.owner_id == chat.seller_id, UserBlock.blocked_id == chat.buyer_id)))) is not None


def feedback_dict(row):
    return dict(rating=row.rating, comment=row.comment, recipient_role=row.recipient_role,
                created_at=row.created_at.isoformat())


def cleanup_message_uploads(db, settings, storage_factory):
    """Remove abandoned upload objects and tickets; sent photos are retained."""
    if not settings.r2_message_bucket_name:
        return
    storage = copy(storage_factory())
    storage.bucket_name = settings.r2_message_bucket_name
    with db.sessions.begin() as session:
        rows = session.scalars(select(Attachment).where(Attachment.used.is_(False),
            Attachment.created_at < utc_now() - timedelta(days=1)).limit(100).with_for_update()).all()
        for row in rows:
            storage.delete_objects([row.object_key, row.object_key.replace("messages/", "message-photos/", 1)])
            session.delete(row)


def install_community(app, settings, require_database, require_user, storage_factory):
    original_storage_factory = storage_factory
    def private_storage():
        if not settings.r2_message_bucket_name:
            raise HTTPException(503, "Message photos need a private R2 bucket. Set R2_MESSAGE_BUCKET_NAME on the server.")
        storage = copy(original_storage_factory())
        storage.bucket_name = settings.r2_message_bucket_name
        storage.url_expiry = 600
        return storage
    storage_factory = private_storage
    @app.post("/api/v1/listings/{listing_id}/conversation")
    def start_chat(listing_id: str, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            existing = session.scalar(select(Conversation).where(Conversation.listing_id == listing_id, Conversation.buyer_id == user["id"]))
            if existing:
                return {"id": existing.id}
            listing = session.get(Listing, listing_id)
            if listing is None or listing.status != "published":
                raise HTTPException(404, "Published listing not found.")
            if listing.seller_id == user["id"]:
                raise HTTPException(400, "This is your listing. Read buyer questions in Account → Messages.")
            chat = Conversation(id=str(uuid4()), listing_id=listing_id, title=listing.title,
                                buyer_id=user["id"], seller_id=listing.seller_id)
            if blocked(session, chat):
                raise HTTPException(403, "Messaging is blocked between these accounts.")
            session.add(chat)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                chat = session.scalar(select(Conversation).where(Conversation.listing_id == listing_id, Conversation.buyer_id == user["id"]))
            return {"id": chat.id}

    @app.get("/api/v1/messages")
    def inbox(offset: int = 0, user=Depends(require_user), db=Depends(require_database)):
        if offset < 0:
            raise HTTPException(400, "Invalid page.")
        with db.sessions() as session:
            chats = session.scalars(select(Conversation).where(or_(Conversation.buyer_id == user["id"], Conversation.seller_id == user["id"])).order_by(Conversation.updated_at.desc(), Conversation.id).offset(offset).limit(30)).all()
            result = []
            for chat in chats:
                other = session.get(User, chat.seller_id if chat.buyer_id == user["id"] else chat.buyer_id)
                result.append(dict(id=chat.id, title=chat.title, other_name=other.display_name if other else "Former user", updated_at=chat.updated_at.isoformat()))
            return result

    @app.get("/api/v1/messages/{conversation_id}")
    def read_chat(conversation_id: str, before: int = 0, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            chat = participant(session, conversation_id, user["id"])
            stmt = select(Message).where(Message.conversation_id == conversation_id)
            if before > 0:
                stmt = stmt.where(Message.id < before)
            rows = session.scalars(stmt.order_by(Message.id.desc()).limit(50)).all()
            result = []
            for row in reversed(rows):
                attachment = session.get(Attachment, row.attachment_id) if row.attachment_id else None
                result.append(dict(id=row.id, mine=row.sender_id == user["id"], body=row.body,
                                   image_url=storage_factory().presign_object(attachment.object_key) if attachment else None,
                                   created_at=row.created_at.isoformat()))
            other_id = chat.seller_id if chat.buyer_id == user["id"] else chat.buyer_id
            other = session.get(User, other_id)
            return dict(title=chat.title, other_name=other.display_name if other else "Former user", messages=result,
                        blocked=blocked(session, chat), blocked_by_me=session.get(UserBlock, (user["id"], other_id)) is not None,
                        next_before=rows[-1].id if len(rows) == 50 else None)

    @app.post("/api/v1/messages/{conversation_id}/block")
    def block_chat(conversation_id: str, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions.begin() as session:
            chat = participant(session, conversation_id, user["id"])
            other = chat.seller_id if chat.buyer_id == user["id"] else chat.buyer_id
            session.merge(UserBlock(owner_id=user["id"], blocked_id=other))
        return {"status": "blocked"}

    @app.delete("/api/v1/messages/{conversation_id}/block")
    def unblock_chat(conversation_id: str, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions.begin() as session:
            chat = participant(session, conversation_id, user["id"])
            other = chat.seller_id if chat.buyer_id == user["id"] else chat.buyer_id
            row = session.get(UserBlock, (user["id"], other))
            if row:
                session.delete(row)
        return {"status": "unblocked"}

    @app.post("/api/v1/messages/{conversation_id}/attachments")
    def attachment_upload(conversation_id: str, payload: AttachmentInput, user=Depends(require_user), db=Depends(require_database)):
        extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        if payload.content_type not in extensions:
            raise HTTPException(400, "Select a JPEG, PNG or WebP photo.")
        with db.sessions.begin() as session:
            chat = participant(session, conversation_id, user["id"])
            if blocked(session, chat):
                raise HTTPException(403, "Messaging is blocked.")
            count = session.scalar(select(func.count()).select_from(Attachment).where(Attachment.sender_id == user["id"], Attachment.created_at > utc_now() - timedelta(hours=1)))
            if count >= 30:
                raise HTTPException(429, "Photo limit reached. Try again later.")
            storage = storage_factory()
            attachment_id = str(uuid4())
            key = f"messages/{conversation_id}/{attachment_id}.{extensions[payload.content_type]}"
            url = storage.client.generate_presigned_url("put_object", Params={"Bucket": storage.bucket_name, "Key": key, "ContentType": payload.content_type}, ExpiresIn=600)
            session.add(Attachment(id=attachment_id, conversation_id=conversation_id, sender_id=user["id"], object_key=key, content_type=payload.content_type, size_bytes=payload.size_bytes))
            return dict(id=attachment_id, upload_url=url)

    @app.post("/api/v1/messages/{conversation_id}")
    def send_message(conversation_id: str, payload: MessageInput, background: BackgroundTasks, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            chat = participant(session, conversation_id, user["id"])
            existing = session.scalar(select(Message).where(Message.conversation_id == conversation_id, Message.sender_id == user["id"], Message.client_id == payload.client_id))
            if existing:
                return {"id": existing.id}
            if blocked(session, chat):
                raise HTTPException(403, "Messaging is blocked.")
            if not payload.body.strip() and not payload.attachment_id:
                raise HTTPException(400, "Add a message or photo.")
            count = session.scalar(select(func.count()).select_from(Message).where(Message.sender_id == user["id"], Message.created_at > utc_now() - timedelta(minutes=1)))
            if count >= 20:
                raise HTTPException(429, "Please wait before sending more messages.")
            attachment = None
            if payload.attachment_id:
                attachment = session.scalar(select(Attachment).where(Attachment.id == payload.attachment_id).with_for_update())
                if attachment is None or attachment.conversation_id != conversation_id or attachment.sender_id != user["id"] or attachment.used:
                    raise HTTPException(400, "Invalid photo attachment.")
                created = attachment.created_at.replace(tzinfo=timezone.utc) if attachment.created_at.tzinfo is None else attachment.created_at
                if created < utc_now() - timedelta(days=1):
                    raise HTTPException(400, "Photo attachment expired. Attach it again.")
                storage = storage_factory()
                try:
                    response = storage.client.get_object(Bucket=storage.bucket_name, Key=attachment.object_key)
                    try:
                        data = response["Body"].read(20 * 1024 * 1024 + 1)
                    finally:
                        response["Body"].close()
                    if len(data) != attachment.size_bytes or len(data) > 20 * 1024 * 1024:
                        raise ValueError("Invalid size")
                    with Image.open(BytesIO(data)) as image:
                        if Image.MIME.get(image.format) != attachment.content_type or image.width * image.height > 40_000_000:
                            raise ValueError("Invalid image")
                        image.verify()
                except Exception:
                    raise HTTPException(400, "Photo upload is incomplete or invalid. Attach it again.")
                # Copy to a new key: an unexpired upload URL cannot change a sent photo.
                final_key = attachment.object_key.replace("messages/", "message-photos/", 1)
                storage.client.put_object(Bucket=storage.bucket_name, Key=final_key, Body=data, ContentType=attachment.content_type, CacheControl="private, max-age=300")
                storage.delete_objects([attachment.object_key])
                attachment.object_key = final_key
                attachment.used = True
            message = Message(conversation_id=conversation_id, sender_id=user["id"], client_id=payload.client_id,
                              body=payload.body.strip(), attachment_id=payload.attachment_id)
            session.add(message)
            chat.updated_at = utc_now()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(Message).where(Message.conversation_id == conversation_id, Message.sender_id == user["id"], Message.client_id == payload.client_id))
                if existing:
                    return {"id": existing.id}
                raise
            recipient = chat.seller_id if chat.buyer_id == user["id"] else chat.buyer_id
            background.add_task(send_push, settings, db, [recipient], "New card message", "You have a new message in Account → Messages.", {"conversation_id": conversation_id, "page": "messages"})
            return {"id": message.id}

    @app.get("/api/v1/orders/{order_id}/feedback")
    def order_feedback(order_id: str, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            order = session.get(Order, order_id)
            if order is None or user["id"] not in (order.buyer_id, order.seller_id):
                raise HTTPException(404, "Order not found.")
            rows = session.scalars(select(Feedback).where(Feedback.order_id == order_id)).all()
            return dict(eligible=order.status in ("completed", "refunded"), reviews=[dict(feedback_dict(r), mine=r.author_id == user["id"]) for r in rows])

    @app.post("/api/v1/orders/{order_id}/feedback")
    def leave_feedback(order_id: str, payload: FeedbackInput, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            order = session.get(Order, order_id)
            if order is None or user["id"] not in (order.buyer_id, order.seller_id):
                raise HTTPException(404, "Order not found.")
            if order.status not in ("completed", "refunded"):
                raise HTTPException(409, "Feedback opens after the transaction completes or is refunded.")
            recipient = order.seller_id if user["id"] == order.buyer_id else order.buyer_id
            session.add(Feedback(id=str(uuid4()), order_id=order_id, author_id=user["id"], recipient_id=recipient,
                                 recipient_role="seller" if recipient == order.seller_id else "buyer", rating=payload.rating, comment=payload.comment.strip()))
            try:
                session.commit()
            except IntegrityError:
                raise HTTPException(409, "You already left feedback for this transaction.")
            return {"status": "saved"}

    @app.get("/api/v1/listings/{listing_id}/seller-feedback")
    def seller_feedback(listing_id: str, offset: int = 0, db=Depends(require_database)):
        with db.sessions() as session:
            listing = session.get(Listing, listing_id)
            if listing is None or listing.status != "published":
                raise HTTPException(404, "Published listing not found.")
            filters = (Feedback.recipient_id == listing.seller_id, Feedback.recipient_role == "seller")
            count, average = session.execute(select(func.count(Feedback.id), func.avg(Feedback.rating)).where(*filters)).one()
            rows = session.scalars(select(Feedback).where(*filters).order_by(Feedback.created_at.desc(), Feedback.id).offset(max(0, offset)).limit(20)).all()
            return dict(count=count, average=round(float(average), 2) if average else None, reviews=[feedback_dict(row) for row in rows])

    @app.get("/api/v1/account/feedback")
    def my_feedback(offset: int = 0, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as session:
            rows = session.scalars(select(Feedback).where(Feedback.recipient_id == user["id"]).order_by(Feedback.created_at.desc(), Feedback.id).offset(max(0, offset)).limit(20)).all()
            return {"reviews": [feedback_dict(row) for row in rows]}
