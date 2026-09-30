from datetime import timedelta
from test_marketplace import market
from test_cart import prepare
from cart import build_orders
from account_health import health, metric, record_dispute
from database import Order, utc_now
from main import app, require_admin


def test_threshold_boundaries():
    assert not metric('tracking',96,100,96,True)['warning']
    assert metric('tracking',95,100,96,True)['warning']
    assert not metric('refunds',1,50,2,False)['warning']
    assert metric('refunds',1,49,2,False)['warning']
    assert not metric('success',19,20,95,True)['warning']
    assert metric('success',18,20,95,True)['warning']
    assert metric('new',0,0,96,True)['percent'] is None


def test_own_health_and_admin_access(market):
    client,db,_,_=market
    result=client.get('/api/v1/account/health')
    assert result.status_code==200 and not result.json()['at_risk']
    assert client.get('/api/v1/admin/account-warnings').status_code==403
    app.dependency_overrides[require_admin]=lambda: {'id':'admin'}
    assert client.get('/api/v1/admin/account-warnings').json()['total']==0


def test_pending_success_not_penalized_overdue_tracking_flagged(market):
    client,db,_,_=market
    prepare(db);_,orders,_=build_orders(db,['card','second'],'other')
    with db.sessions.begin() as s:
        o=s.get(Order,orders[0]['id']);o.status='paid';o.paid_at=utc_now()-timedelta(days=10)
    result=client.get('/api/v1/account/health').json()
    assert result['at_risk'] and result['metrics'][0]['percent']==0
    assert result['metrics'][3]['percent'] is None
    app.dependency_overrides[require_admin]=lambda: {'id':'admin'}
    warnings=client.get('/api/v1/admin/account-warnings').json()
    assert warnings['total']==1 and warnings['items'][0]['id']=='seller'


def test_disputes_idempotent_ordered_and_shared_cart_charge(market):
    _,db,_,_=market
    prepare(db);_,orders,_=build_orders(db,['card','second'],'other')
    with db.sessions.begin() as s:
        for item in orders:
            o=s.get(Order,item['id']);o.status='completed';o.payout_status='paid';o.paid_at=utc_now();o.stripe_payment_intent_id='pi_cart'
    record_dispute(db,{'id':'dp_1','status':'needs_response'},10,'pi_cart')
    record_dispute(db,{'id':'dp_1','status':'lost'},20,'pi_cart')
    record_dispute(db,{'id':'dp_1','status':'needs_response'},10,'pi_cart')
    with db.sessions() as s:
        result=health(s,'seller')
    assert result['metrics'][2]['numerator']==2
    assert result['metrics'][1]['numerator']==2
    assert result['metrics'][3]['percent']==0
