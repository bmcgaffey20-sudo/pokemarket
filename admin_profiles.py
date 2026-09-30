"""Admin-only paginated user investigation; never exposes auth secrets."""
from datetime import timezone
from fastapi import Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from database import User, Order, Listing, utc_now
from community import Conversation, Message, Attachment
from account_health import health


def install_admin_profiles(app, require_database, require_admin, stripe_status, storage_factory):
    def page(items, total, offset, limit):
        return dict(items=items,total=total,offset=offset,limit=limit)
    def ensure(s, uid):
        user=s.get(User,uid)
        if not user: raise HTTPException(404,'User not found.')
        return user

    @app.get('/api/v1/admin/user-directory')
    def directory(q: str = Query('',max_length=200), limit: int = Query(20,ge=1,le=50), offset: int = Query(0,ge=0),
                  admin=Depends(require_admin), db=Depends(require_database)):
        with db.sessions() as s:
            condition=or_(User.email.icontains(q.strip(),autoescape=True),User.display_name.icontains(q.strip(),autoescape=True),User.id==q.strip())
            total=s.scalar(select(func.count()).select_from(User).where(condition))
            rows=s.scalars(select(User).where(condition).order_by(User.created_at.desc(),User.id).offset(offset).limit(limit)).all()
            return page([dict(id=u.id,display_name=u.display_name,email=u.email,seller_tier=u.seller_tier,is_admin=u.is_admin) for u in rows],total,offset,limit)

    @app.get('/api/v1/admin/user-directory/{uid}')
    async def profile(uid: str,admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions() as s:
            u=ensure(s,uid); data=db._user_dict(u);data['health']=health(s,uid)
            created=u.created_at.replace(tzinfo=timezone.utc) if u.created_at.tzinfo is None else u.created_at
            data['account_age_days']=max(0,(utc_now()-created).days)
        data=await stripe_status(data)
        # This is Connect readiness, not a separate authenticity/identity guarantee.
        data['stripe_status']=('ready' if data.get('stripe_connected') else 'not_ready') if data.get('stripe_status_checked') else ('unknown' if data.get('stripe_account_id') else 'not_connected')
        return data

    @app.get('/api/v1/admin/user-directory/{uid}/purchases')
    def purchases(uid: str,limit: int=Query(20,ge=1,le=50),offset: int=Query(0,ge=0),admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions() as s:
            ensure(s,uid);condition=Order.buyer_id==uid
            total=s.scalar(select(func.count()).select_from(Order).where(condition))
            rows=s.scalars(select(Order).where(condition).order_by(Order.created_at.desc(),Order.id).offset(offset).limit(limit)).all()
            return page([dict(id=o.id,status=o.status,item_cents=o.item_cents,shipping_cents=o.shipping_cents,currency=o.currency,created_at=o.created_at,listing_title=(s.get(Listing,o.listing_id).title if s.get(Listing,o.listing_id) else "Deleted listing")) for o in rows],total,offset,limit)

    @app.get('/api/v1/admin/user-directory/{uid}/messages')
    def messages(uid: str,limit: int=Query(20,ge=1,le=50),offset: int=Query(0,ge=0),admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions() as s:
            ensure(s,uid);condition=or_(Conversation.buyer_id==uid,Conversation.seller_id==uid)
            total=s.scalar(select(func.count()).select_from(Conversation).where(condition))
            rows=s.scalars(select(Conversation).where(condition).order_by(Conversation.updated_at.desc(),Conversation.id).offset(offset).limit(limit)).all()
            return page([dict(id=c.id,title=c.title,buyer_id=c.buyer_id,seller_id=c.seller_id,updated_at=c.updated_at) for c in rows],total,offset,limit)

    @app.get('/api/v1/admin/user-directory/{uid}/messages/{cid}')
    def thread(uid: str,cid: str,limit: int=Query(50,ge=1,le=50),offset: int=Query(0,ge=0),admin=Depends(require_admin),db=Depends(require_database)):
        with db.sessions() as s:
            ensure(s,uid);c=s.get(Conversation,cid)
            if not c or uid not in {c.buyer_id,c.seller_id}:raise HTTPException(404,'Conversation not found for this user.')
            total=s.scalar(select(func.count()).select_from(Message).where(Message.conversation_id==cid))
            rows=s.scalars(select(Message).where(Message.conversation_id==cid).order_by(Message.created_at,Message.id).offset(offset).limit(limit)).all()
            result=[]
            for m in rows:
                sender=s.get(User,m.sender_id);att=s.get(Attachment,m.attachment_id) if m.attachment_id else None
                result.append(dict(id=m.id,sender_id=m.sender_id,sender_name=sender.display_name if sender else 'Deleted user',body=m.body,created_at=m.created_at,image_url=storage_factory().presign_object(att.object_key) if att else None))
            return dict(title=c.title,**page(result,total,offset,limit))
