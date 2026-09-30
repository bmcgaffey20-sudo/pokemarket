from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Column,Integer,String,Text,DateTime,UniqueConstraint,select,func
from database import Base,utc_now,User
from community import Message,Conversation

class MessageReport(Base):
    __tablename__='message_reports'
    __table_args__=(UniqueConstraint('reporter_id','message_id',name='uq_message_report'),)
    id=Column(Integer,primary_key=True,autoincrement=True)
    reporter_id=Column(String(36),nullable=False)
    sender_id=Column(String(36),nullable=False)
    message_id=Column(Integer,nullable=False)
    conversation_id=Column(String(36),nullable=False)
    body=Column(Text,nullable=False)
    attachment_id=Column(String(36))
    reason=Column(String(1000),nullable=False)
    status=Column(String(24),nullable=False,default='open')
    created_at=Column(DateTime(timezone=True),default=utc_now,nullable=False)

class ReportInput(BaseModel):
    reason:str=Field(min_length=3,max_length=1000)


def install_reports(app,require_database,require_user,require_admin):
    @app.post('/api/v1/messages/{cid}/report/{mid}')
    def report(cid:str,mid:int,payload:ReportInput,user=Depends(require_user),db=Depends(require_database)):
        reason=payload.reason.strip()
        if len(reason)<3:raise HTTPException(400,'Please describe the concern.')
        with db.sessions.begin() as s:
            # User lock serializes duplicate submissions.
            s.get(User,user['id'],with_for_update=True)
            c=s.get(Conversation,cid);m=s.get(Message,mid)
            if not c or user['id'] not in {c.buyer_id,c.seller_id} or not m or m.conversation_id!=cid:raise HTTPException(404,'Message not found.')
            if m.sender_id==user['id']:raise HTTPException(400,'Report a received message.')
            old=s.scalar(select(MessageReport).where(MessageReport.reporter_id==user['id'],MessageReport.message_id==mid))
            if old:return dict(id=old.id,status=old.status)
            row=MessageReport(reporter_id=user['id'],sender_id=m.sender_id,message_id=mid,conversation_id=cid,body=m.body,attachment_id=m.attachment_id,reason=reason)
            s.add(row);s.flush();return dict(id=row.id,status=row.status)

    @app.get('/api/v1/admin/message-reports')
    def reports(offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50),admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions() as s:
            total=s.scalar(select(func.count()).select_from(MessageReport))
            rows=s.scalars(select(MessageReport).order_by(MessageReport.created_at.desc(),MessageReport.id.desc()).offset(offset).limit(limit)).all()
            return dict(total=total,items=[dict(id=r.id,reporter_id=r.reporter_id,sender_id=r.sender_id,message_id=r.message_id,conversation_id=r.conversation_id,body=r.body,reason=r.reason,status=r.status,created_at=r.created_at) for r in rows])

    @app.post('/api/v1/admin/message-reports/{rid}/resolve')
    def resolve(rid:int,admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions.begin() as s:
            row=s.get(MessageReport,rid)
            if not row:raise HTTPException(404,'Report not found.')
            row.status='reviewed'
        return dict(status='reviewed')
