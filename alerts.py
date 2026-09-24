from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, select, func, or_, update
from database import Base, utc_now
from community import Conversation, Message, participant

class Notification(Base):
    __tablename__ = "user_notifications"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    page = Column(String(32), nullable=False, default="orders")
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

class MessageRead(Base):
    __tablename__ = "message_read_cursors"
    user_id = Column(String(36), primary_key=True)
    conversation_id = Column(String(36), primary_key=True)
    through_id = Column(Integer, nullable=False, default=0)

class ReadInput(BaseModel):
    through_id: int = Field(ge=0)

def record_notifications(db, user_ids, title, body, data):
    if data.get("conversation_id"):
        return
    with db.sessions.begin() as session:
        for uid in set(user_ids):
            session.add(Notification(user_id=uid, title=title[:200], body=body,
                page=data.get("page") if data.get("page") in {"orders", "account", "admin"} else "orders"))

def install_alerts(app, require_database, require_user):
    @app.get("/api/v1/activity/unread")
    def unread(user=Depends(require_user), db=Depends(require_database)):
        uid = user["id"]
        with db.sessions() as session:
            cursor = select(MessageRead.through_id).where(MessageRead.user_id == uid,
                MessageRead.conversation_id == Message.conversation_id).correlate(Message).scalar_subquery()
            owned = or_(Conversation.buyer_id == uid, Conversation.seller_id == uid)
            messages = session.scalar(select(func.count()).select_from(Message).join(Conversation, Message.conversation_id == Conversation.id).where(
                owned, Message.sender_id != uid, Message.id > func.coalesce(cursor, 0)))
            notifications = session.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == uid, Notification.is_read.is_(False)))
            latest = session.scalar(select(func.max(Message.id)).join(Conversation, Message.conversation_id == Conversation.id).where(owned)) or 0
            latest_notification = session.scalar(select(func.max(Notification.id)).where(Notification.user_id == uid)) or 0
            return {"messages": messages, "notifications": notifications, "latest_message_id": latest, "latest_notification_id": latest_notification}

    @app.post("/api/v1/messages/{conversation_id}/read")
    def read_messages(conversation_id: str, payload: ReadInput, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions.begin() as session:
            # Serialize first-time cursor creation as well as subsequent updates.
            chat = participant(session, conversation_id, user["id"])
            session.execute(select(Conversation).where(Conversation.id == chat.id).with_for_update()).scalar_one()
            latest = session.scalar(select(func.max(Message.id)).where(Message.conversation_id == conversation_id)) or 0
            value = min(payload.through_id, latest)
            row = session.get(MessageRead, (user["id"], conversation_id))
            if row:
                row.through_id = max(row.through_id, value)
            else:
                session.add(MessageRead(user_id=user["id"], conversation_id=conversation_id, through_id=value))
        return {"status": "read"}

    @app.get("/api/v1/notifications")
    def notifications(offset: int = 0, user=Depends(require_user), db=Depends(require_database)):
        if offset < 0: raise HTTPException(400, "Invalid page.")
        with db.sessions() as session:
            rows = session.scalars(select(Notification).where(Notification.user_id == user["id"]).order_by(Notification.id.desc()).offset(offset).limit(30)).all()
            return [dict(id=r.id, title=r.title, body=r.body, page=r.page, is_read=r.is_read, created_at=r.created_at.isoformat()) for r in rows]

    @app.post("/api/v1/notifications/read")
    def read_notifications(payload: ReadInput, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions.begin() as session:
            session.execute(update(Notification).where(Notification.user_id == user["id"], Notification.id <= payload.through_id).values(is_read=True))
        return {"status": "read"}

    @app.post("/api/v1/notifications/{notification_id}/read")
    def read_notification(notification_id: int, user=Depends(require_user), db=Depends(require_database)):
        with db.sessions.begin() as session:
            row = session.get(Notification, notification_id)
            if row is None or row.user_id != user["id"]: raise HTTPException(404, "Notification not found.")
            row.is_read = True
        return {"status": "read"}
