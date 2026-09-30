"""Persistent collections and atomic mixed-seller checkout settlement."""
import asyncio
import uuid
from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Integer, Text, select
from database import Base, Listing, Order, User, utc_now
from notifications import send_push

class CollectionItem(Base):
    __tablename__ = 'collection_items'
    user_id = Column(String(36), primary_key=True)
    listing_id = Column(String(64), primary_key=True)
    kind = Column(String(16), primary_key=True)

class CartPayment(Base):
    __tablename__ = 'cart_payments'
    id = Column(String(36), primary_key=True)
    buyer_id = Column(String(36), nullable=False)
    session_id = Column(String(128), unique=True)
    state = Column(String(32), default='pending', nullable=False)
    total_cents = Column(Integer, nullable=False)
    refund_id = Column(String(128))

class CartOrder(Base):
    __tablename__ = 'cart_orders'
    payment_id = Column(String(36), primary_key=True)
    order_id = Column(String(36), primary_key=True)

class CartInput(BaseModel):
    listing_ids: list[str] = Field(min_length=1, max_length=30)


def build_orders(db, ids, user_id):
    from account_controls import require_active_transaction
    ids = sorted(set(ids))
    with db.sessions.begin() as session:
        require_active_transaction(session, user_id)
        listings = session.scalars(select(Listing).where(Listing.id.in_(ids)).order_by(Listing.id).with_for_update()).all()
        if len(listings) != len(ids) or any(l.status != 'published' or not l.publication_approved or l.seller_id == user_id or l.currency != 'USD' or (l.price_cents or 0) < 900 for l in listings):
            raise HTTPException(409, 'Some cards are unavailable, belong to you, or are priced below $9.')
        if sum(l.price_cents for l in listings) < 1900:
            raise HTTPException(409, 'The merchandise subtotal must be at least $19.00, excluding shipping.')
        sellers = {l.seller_id: session.get(User, l.seller_id) for l in listings}
        for seller in sellers.values():
            require_active_transaction(session, seller.id)
            if not seller.stripe_account_id or not db._seller_address_dict(seller)['configured']:
                raise HTTPException(409, 'A seller has not completed payout or shipping setup.')
        payment = CartPayment(id=str(uuid.uuid4()), buyer_id=user_id, total_cents=sum(l.price_cents + 499 for l in listings))
        session.add(payment)
        total = sum(l.price_cents for l in listings)
        fees = (total * 8 + 99) // 100 + 30
        allocated = 0
        result = []
        for i,l in enumerate(listings):
            # Allocate the checkout-level fee proportionally. Total is exact, including one 30-cent fee.
            fee = fees - allocated if i == len(listings)-1 else fees * l.price_cents // total
            allocated += fee
            a = db._seller_address_dict(sellers[l.seller_id])
            o = Order(id=str(uuid.uuid4()),listing_id=l.id,buyer_id=user_id,seller_id=l.seller_id,item_cents=l.price_cents,shipping_cents=499,commission_cents=fee,seller_amount_cents=l.price_cents+499-fee,currency='USD')
            for key in ('name','line1','line2','city','state','postal_code','country'): setattr(o,'seller_shipping_'+key,a[key] or None)
            session.add(o)
            session.add(CartOrder(payment_id=payment.id,order_id=o.id))
            session.flush()
            result.append(db._order_dict(o,l))
        return payment.id, result, {sid:u.stripe_account_id for sid,u in sellers.items()}


def claim_cart(db, session_id, intent, shipping):
    with db.sessions.begin() as s:
        payment=s.scalar(select(CartPayment).where(CartPayment.session_id==session_id).with_for_update())
        if not payment: return None
        if payment.state in ('paid','refunded'): return {'duplicate':True}
        order_ids=s.scalars(select(CartOrder.order_id).where(CartOrder.payment_id==payment.id)).all()
        orders=s.scalars(select(Order).where(Order.id.in_(order_ids)).order_by(Order.id).with_for_update()).all()
        listings=s.scalars(select(Listing).where(Listing.id.in_([o.listing_id for o in orders])).order_by(Listing.id).with_for_update()).all()
        by_id={l.id:l for l in listings}
        won=payment.state != 'refund_pending' and all(by_id[o.listing_id].status=='published' and by_id[o.listing_id].publication_approved for o in orders)
        payment.state='paid' if won else 'refund_pending'
        for o in orders:
            o.stripe_payment_intent_id=intent
            o.status='paid' if won else 'refund_pending'
            if won:
                o.paid_at=utc_now()
                l=by_id[o.listing_id]; l.status='sold';l.publication_approved=False
                a=shipping.get('address') or {}
                o.shipping_name=shipping.get('name')
                for key in ('line1','line2','city','state','postal_code','country'): setattr(o,'shipping_'+key,a.get(key))
                s.query(CollectionItem).filter(CollectionItem.user_id==payment.buyer_id,CollectionItem.listing_id==o.listing_id,CollectionItem.kind=='cart').delete()
                db._audit(s,o.id,'payment_confirmed',payment.buyer_id)
            else: o.payout_status='not_applicable'
        return {'id':payment.id,'won':won,'orders':[db._order_dict(o,by_id[o.listing_id]) for o in orders]}

async def cart_webhook(checkout, db, stripe, settings, background):
    result=await asyncio.to_thread(claim_cart,db,checkout.get('id'),checkout.get('payment_intent'),checkout.get('shipping_details') or (checkout.get('collected_information') or {}).get('shipping_details') or {})
    if result is None: return False
    if result.get('duplicate'): return True
    if result['won']:
        for o in result['orders']:
            background.add_task(send_push,settings,db,[o['seller_id']],'Card sold',f"{o['listing_title']} sold. Provide tracking within 3 business days.",{'page':'orders','order_id':o['id']})
    else:
        refund=await asyncio.to_thread(stripe.Refund.create,payment_intent=checkout['payment_intent'],idempotency_key='cart-refund-'+result['id'])
        with db.sessions.begin() as s:
            p=s.get(CartPayment,result['id'],with_for_update=True);p.state='refunded';p.refund_id=refund['id']
            for o in result['orders']:
                row=s.get(Order,o['id']);row.status='refunded';row.stripe_refund_id=refund['id']
    return True


def install_cart(app,settings,require_database,require_user,require_stripe):
    @app.get('/api/v1/collections/{kind}')
    def collection(kind:str,user=Depends(require_user),db=Depends(require_database)):
        if kind not in ('cart','watchlist'): raise HTTPException(404)
        with db.sessions() as s:
            rows=s.execute(select(Listing).join(CollectionItem,CollectionItem.listing_id==Listing.id).where(CollectionItem.user_id==user['id'],CollectionItem.kind==kind)).scalars().all()
            return [{'id':l.id,'title':l.title,'price_cents':l.price_cents,'currency':l.currency,'status':l.status,'seller_id':l.seller_id} for l in rows]

    @app.put('/api/v1/collections/{kind}/{listing_id}')
    def add(kind:str,listing_id:str,user=Depends(require_user),db=Depends(require_database)):
        if kind not in ('cart','watchlist'): raise HTTPException(404)
        with db.sessions.begin() as s:
            # Serializing on user makes duplicate additions safe.
            s.get(User,user['id'],with_for_update=True)
            l=s.get(Listing,listing_id)
            if not l or l.status!='published' or not l.publication_approved: raise HTTPException(409,'Listing unavailable.')
            if kind=='cart' and l.seller_id==user['id']: raise HTTPException(409,'You cannot buy your own card.')
            if not s.get(CollectionItem,(user['id'],listing_id,kind)): s.add(CollectionItem(user_id=user['id'],listing_id=listing_id,kind=kind))
        return {'status':'saved'}

    @app.delete('/api/v1/collections/{kind}/{listing_id}')
    def remove(kind:str,listing_id:str,user=Depends(require_user),db=Depends(require_database)):
        if kind not in ('cart','watchlist'): raise HTTPException(404)
        with db.sessions.begin() as s:
            row=s.get(CollectionItem,(user['id'],listing_id,kind))
            if row:s.delete(row)
        return {'status':'removed'}

    @app.post('/api/v1/cart/checkout')
    async def checkout(payload:CartInput,user=Depends(require_user),db=Depends(require_database)):
        stripe=require_stripe()
        pid,orders,sellers=await asyncio.to_thread(build_orders,db,payload.listing_ids,user['id'])
        try:
            for account in sellers.values():
                a=await asyncio.to_thread(stripe.Account.retrieve,account)
                if not a.get('charges_enabled') or not a.get('payouts_enabled'): raise HTTPException(409,'A seller payout account is not ready.')
            base=settings.public_base_url.rstrip('/')
            items=[{'price_data':{'currency':'usd','product_data':{'name':o['listing_title']},'unit_amount':o['item_cents']},'quantity':1} for o in orders]
            items.append({'price_data':{'currency':'usd','product_data':{'name':'Shipping ($4.99 per card)'},'unit_amount':499},'quantity':len(orders)})
            result=await asyncio.to_thread(stripe.checkout.Session.create,mode='payment',line_items=items,success_url=base+'/checkout/success?session_id={CHECKOUT_SESSION_ID}',cancel_url=base+'/checkout/cancelled',customer_email=user['email'],shipping_address_collection={'allowed_countries':['US']},metadata={'cart_payment_id':pid},payment_intent_data={'metadata':{'cart_payment_id':pid}})
            with db.sessions.begin() as s:
                s.get(CartPayment,pid).session_id=result['id']
                for o in orders:s.get(Order,o['id']).stripe_checkout_session_id=result['id']+'#'+o['id']
            return {'checkout_url':result['url'],'item_cents':sum(o['item_cents'] for o in orders),'shipping_cents':499*len(orders)}
        except Exception as exc:
            with db.sessions.begin() as s:
                s.get(CartPayment,pid).state='canceled'
                for o in orders:s.get(Order,o['id']).status='canceled'
            if isinstance(exc,HTTPException):raise
            raise HTTPException(502,'Unable to create cart checkout. Retry shortly.') from exc
