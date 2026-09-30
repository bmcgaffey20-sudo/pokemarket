from types import SimpleNamespace
from datetime import timedelta
from test_marketplace import market
from test_cart import prepare
from main import app,require_admin
from database import Order,Listing,utc_now
from instant_sales import InstantOffer,offer_rule


def setup(market,monkeypatch,value=10000):
    client,db,user,_=market;prepare(db)
    async def pricing(settings,data):return {'estimate_cents':value,'confidence':'medium','rationale':'Model estimate only'}
    monkeypatch.setattr('instant_sales.estimate',pricing)
    admin=db.get_user('other');app.dependency_overrides[require_admin]=lambda:admin
    class Checkout:
        calls=[]
        @classmethod
        def create(cls,**kw):cls.calls.append(kw);return {'id':'cs_instant','url':'https://checkout.stripe.com/test'}
    monkeypatch.setattr('main.require_stripe',lambda:SimpleNamespace(Account=SimpleNamespace(retrieve=lambda _:dict(charges_enabled=True,payouts_enabled=True)),checkout=SimpleNamespace(Session=Checkout)))
    return client,db,Checkout


def test_private_estimate_policies_fee_zero_funded_order(market,monkeypatch):
    c,db,checkout=setup(market,monkeypatch)
    response=c.post('/api/v1/listings/card/instant-offer');assert response.status_code==200,response.text
    offer=response.json();assert offer['offer_cents']==2900 and offer['fee_cents']==0
    assert all(k not in offer for k in ['estimate_cents','rationale','confidence','snapshot'])
    assert c.post('/api/v1/listings/card/instant-offer').json()['id']==offer['id']
    rid=offer['id']
    assert c.post('/api/v1/instant-offers/'+rid+'/decision',json={'accepted':True}).status_code==400
    assert c.post('/api/v1/instant-offers/'+rid+'/decision',json={'accepted':True,'policies_accepted':True}).status_code==200
    lead=c.get('/api/v1/admin/instant-offers/'+rid).json();assert lead['estimate_cents']==10000
    approval=c.post('/api/v1/admin/instant-offers/'+rid+'/approve');assert approval.status_code==200,approval.text
    assert len(checkout.calls)==1 and checkout.calls[0]['line_items'][0]['price_data']['unit_amount']==2900
    assert c.post('/api/v1/admin/instant-offers/'+rid+'/approve').status_code==200 and len(checkout.calls)==1
    oid=approval.json()['order_id']
    with db.sessions() as s:
        o=s.get(Order,oid);assert o.status=='pending_payment' and o.commission_cents==0 and o.seller_amount_cents==3399
        assert s.get(Listing,'card').price_cents==1250
    paid=db.claim_order_payment('cs_instant','pi_instant');assert paid['won']
    confirmed=db.confirm_delivery(oid,'other',10)
    assert (confirmed['hold_until']-confirmed['delivered_at']).days==10


def test_stale_listing_never_fills_instant_order(market,monkeypatch):
    c,db,_=setup(market,monkeypatch)
    rid=c.post('/api/v1/listings/card/instant-offer').json()['id']
    c.post('/api/v1/instant-offers/'+rid+'/decision',json={'accepted':True,'policies_accepted':True})
    assert c.post('/api/v1/admin/instant-offers/'+rid+'/approve').status_code==200
    with db.sessions.begin() as s:s.get(Listing,'card').card_name='Different card'
    r=db.claim_order_payment('cs_instant','pi_changed')
    assert not r['won'] and r['order']['status']=='refund_pending'


def test_offer_rules_boundaries_and_damage_override():
    def data(condition,**extra):return dict(condition=condition,**extra)
    assert offer_rule(data('Mint'),10000)[0]==29
    assert offer_rule(data('Near Mint'),10000)[0]==29
    assert offer_rule(data('Lightly Played'),10000)[0]==24
    assert 'not eligible' in offer_rule(data('Moderately Played'),50000)[1]
    assert offer_rule(data('Moderately Played'),50001)[1]=='Condition requires review'
    assert offer_rule(data('Mint',grading_status='graded'),10000)[0] is None
    damaged=data('Near Mint',ai_result={'condition':{'structural_damage_detected':True}})
    assert 'not eligible' in offer_rule(damaged,50000)[1]
    assert offer_rule(damaged,50001)[1]=='Damage requires review'


def test_graded_review_counter_requires_seller_consent(market,monkeypatch):
    c,db,_=setup(market,monkeypatch)
    with db.sessions.begin() as s:s.get(Listing,'card').grading_status='graded'
    r=c.post('/api/v1/listings/card/instant-offer?review_requested=true');assert r.status_code==200,r.text
    offer=r.json();rid=offer['id'];assert offer['status']=='pending_review' and offer['offer_cents'] is None
    assert c.post('/api/v1/admin/instant-offers/'+rid+'/approve').status_code==409
    r=c.post('/api/v1/admin/instant-offers/'+rid+'/counter',json={'offer_cents':1500});assert r.status_code==200,r.text
    # Below $19 is allowed only for this private purchase flow.
    assert c.post('/api/v1/admin/instant-offers/'+rid+'/approve').status_code==409
    r=c.post('/api/v1/listings/card/instant-offer').json();assert r['offer_cents']==1500 and 'estimate_cents' not in r
    assert c.post('/api/v1/instant-offers/'+rid+'/decision',json={'accepted':True,'policies_accepted':True}).status_code==200
    assert c.post('/api/v1/admin/instant-offers/'+rid+'/approve').status_code==200


def test_decline_expiry_permissions(market,monkeypatch):
    c,db,_=setup(market,monkeypatch,9999)
    o=c.post('/api/v1/listings/card/instant-offer').json();assert o['offer_cents']==2899
    assert c.post('/api/v1/instant-offers/'+o['id']+'/decision',json={'accepted':False}).json()['status']=='seller_declined'
    assert c.post('/api/v1/admin/instant-offers/'+o['id']+'/approve').status_code==409
    app.dependency_overrides.pop(require_admin)
    assert c.get('/api/v1/admin/instant-offers').status_code==403
    with db.sessions.begin() as s:
        row=s.get(InstantOffer,o['id']);row.status='quoted';row.expires_at=utc_now()-timedelta(days=1)
    assert c.post('/api/v1/instant-offers/'+o['id']+'/decision',json={'accepted':True,'policies_accepted':True}).status_code==409


def test_worse_condition_review_only_strictly_over_500(market,monkeypatch):
    c,db,_=setup(market,monkeypatch,50000)
    with db.sessions.begin() as s:s.get(Listing,'card').estimated_condition='Heavily Played'
    r=c.post('/api/v1/listings/card/instant-offer').json()
    assert r['status']=='not_eligible' and r['offer_cents'] is None
    async def high(settings,data):return {'estimate_cents':50001,'confidence':'medium','rationale':'Model estimate'}
    monkeypatch.setattr('instant_sales.estimate',high)
    with db.sessions.begin() as s:s.get(InstantOffer,r['id']).created_at=utc_now()-timedelta(minutes=10)
    r=c.post('/api/v1/listings/card/instant-offer').json()
    assert r['status']=='review_available' and r['offer_cents'] is None
    assert c.post('/api/v1/instant-offers/'+r['id']+'/decision',json={'accepted':True,'policies_accepted':True}).json()['status']=='pending_review'


def test_graded_review_survives_ai_outage(market,monkeypatch):
    c,db,_=setup(market,monkeypatch)
    with db.sessions.begin() as s:s.get(Listing,'card').grading_status='graded'
    async def unavailable(settings,data):raise RuntimeError('AI capacity')
    monkeypatch.setattr('instant_sales.estimate',unavailable)
    r=c.post('/api/v1/listings/card/instant-offer?review_requested=true')
    assert r.status_code==200 and r.json()['status']=='pending_review'
    lead=c.get('/api/v1/admin/instant-offers/'+r.json()['id']).json()
    assert lead['estimate_cents'] is None and lead['offer_cents'] is None


def test_lp_offer_and_wrong_owner_and_sold_card(market,monkeypatch):
    from main import require_user
    c,db,_=setup(market,monkeypatch)
    with db.sessions.begin() as s:s.get(Listing,'card').estimated_condition='Lightly Played'
    r=c.post('/api/v1/listings/card/instant-offer').json();assert r['offer_cents']==2400
    app.dependency_overrides[require_user]=lambda:db.get_user('other')
    assert c.post('/api/v1/listings/card/instant-offer').status_code==404
    assert c.post('/api/v1/instant-offers/'+r['id']+'/decision',json={'accepted':True,'policies_accepted':True}).status_code==404
