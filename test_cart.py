from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from test_marketplace import market
from database import Listing, Order
from cart import build_orders, claim_cart, CartPayment
from shipping_performance import deadline, award, ShippingPerformance


def prepare(db):
    with db.sessions.begin() as s:
        from database import User
        s.get(User,'seller').stripe_account_id='acct_seller'
        l=s.get(Listing,'card');l.status='published';l.publication_approved=True
        s.add(Listing(id='second',seller_id='seller',title='Second',price_cents=900,currency='USD',status='published',publication_approved=True))


def test_cart_totals_shipping_and_once_per_checkout_fee(market):
    client,db,user,images=market
    prepare(db)
    pid,orders,sellers=build_orders(db,['card','second','card'],'other')
    assert sum(o['item_cents'] for o in orders)==2150
    assert sum(o['shipping_cents'] for o in orders)==998
    assert sum(o['commission_cents'] for o in orders)==202
    assert sum(o['seller_amount_cents'] for o in orders)==2946
    with db.sessions.begin() as s:s.get(CartPayment,pid).session_id='cs_cart'
    result=claim_cart(db,'cs_cart','pi_cart',{'name':'Buyer','address':{'line1':'1 Main','country':'US'}})
    assert result['won'] and len(result['orders'])==2
    assert claim_cart(db,'cs_cart','pi_cart',{})['duplicate']
    with db.sessions() as s:assert s.get(Listing,'card').status=='sold'


def test_cart_minimum_excludes_shipping_and_losing_item_refunds_all(market):
    _,db,_,_=market
    prepare(db)
    with pytest.raises(HTTPException):build_orders(db,['second'],'other')
    pid,orders,_=build_orders(db,['card','second'],'other')
    with db.sessions.begin() as s:
        s.get(CartPayment,pid).session_id='cs_race';s.get(Listing,'second').status='sold'
    result=claim_cart(db,'cs_race','pi_race',{})
    assert not result['won']
    with db.sessions() as s:
        assert s.get(Listing,'card').status=='published'
        assert all(s.get(Order,o['id']).status=='refund_pending' for o in orders)


def test_shipping_business_deadline_and_award_once(market):
    _,db,_,_=market
    friday=datetime(2026,10,2,20,tzinfo=timezone.utc)
    assert deadline(friday).astimezone(__import__('zoneinfo').ZoneInfo('America/Los_Angeles')).date().isoformat()=='2026-10-07'
    prepare(db);_,orders,_=build_orders(db,['card','second'],'other')
    with db.sessions.begin() as s:
        from database import utc_now
        o=s.get(Order,orders[0]['id']);o.paid_at=utc_now();award(s,o);s.flush();award(s,o)
    with db.sessions() as s:assert s.get(ShippingPerformance,orders[0]['id']).points==2


def test_mixed_sellers_have_separate_payouts(market):
    _,db,_,_=market
    prepare(db)
    db.create_user('third','third@example.com','Third','hash','salt',10000)
    db.update_seller_address('other', {'name':'Other','line1':'2 Main','line2':'','city':'Pasco','state':'WA','postal_code':'99301','country':'US'})
    with db.sessions.begin() as s:
        from database import User
        s.get(User,'other').stripe_account_id='acct_other'
        s.get(Listing,'second').seller_id='other'
    _,orders,sellers=build_orders(db,['card','second'],'third')
    assert set(sellers)=={'seller','other'}
    assert len({o['seller_id'] for o in orders})==2
    assert sum(o['commission_cents'] for o in orders)==202
    assert all(o['shipping_cents']==499 for o in orders)


def test_collections_and_minimum_single_buy(market):
    client,db,_,_=market
    prepare(db)
    assert client.put('/api/v1/collections/watchlist/card').status_code==200
    assert client.put('/api/v1/collections/watchlist/card').status_code==200
    assert len(client.get('/api/v1/collections/watchlist').json())==1
    assert client.post('/api/v1/payments/checkout',json={'listing_id':'card','shipping_cents':0}).status_code==409
