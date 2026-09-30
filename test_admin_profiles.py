from test_marketplace import market
from main import app,require_admin
from community import Conversation,Message
from test_cart import prepare
from cart import build_orders


def admin():app.dependency_overrides[require_admin]=lambda:{'id':'admin'}


def test_directory_permissions_pagination_search_secrets(market):
    client,db,_,_=market
    assert client.get('/api/v1/admin/user-directory').status_code==403
    admin()
    assert client.get('/api/v1/admin/user-directory?limit=51').status_code==422
    first=client.get('/api/v1/admin/user-directory?limit=1').json()
    second=client.get('/api/v1/admin/user-directory?limit=1&offset=1').json()
    assert first['total']==2 and first['items'][0]['id']!=second['items'][0]['id']
    assert client.get('/api/v1/admin/user-directory?q=seller@example.com').json()['total']==1
    assert client.get('/api/v1/admin/user-directory?q=%25').json()['total']==0
    p=client.get('/api/v1/admin/user-directory/seller').json()
    assert p['stripe_status']=='not_connected' and 'health' in p and p['account_age_days']>=0
    assert 'password_hash' not in p and 'password_salt' not in p


def seed(db):
    with db.sessions.begin() as s:
        s.add(Conversation(id='chat',listing_id='card',title='Card',buyer_id='other',seller_id='seller'))
        s.add(Message(id=1,conversation_id='chat',sender_id='other',client_id='one',body='Bad message'))
        s.add(Message(id=2,conversation_id='chat',sender_id='seller',client_id='two',body='Reply'))


def test_reporting_and_admin_investigation(market):
    client,db,_,_=market;seed(db)
    assert client.post('/api/v1/messages/chat/report/2',json={'reason':'Abuse'}).status_code==400
    assert client.post('/api/v1/messages/wrong/report/1',json={'reason':'Abuse'}).status_code==404
    r=client.post('/api/v1/messages/chat/report/1',json={'reason':'Abusive language'})
    assert r.status_code==200
    assert client.post('/api/v1/messages/chat/report/1',json={'reason':'Abusive language'}).json()['id']==r.json()['id']
    assert client.get('/api/v1/admin/message-reports').status_code==403
    admin()
    reports=client.get('/api/v1/admin/message-reports').json()
    assert reports['total']==1 and reports['items'][0]['body']=='Bad message'
    thread=client.get('/api/v1/admin/user-directory/seller/messages/chat').json()
    assert len(thread['items'])==2
    assert client.post('/api/v1/admin/message-reports/1/resolve').json()['status']=='reviewed'


def test_history_pagination_and_purchase_details(market):
    client,db,_,_=market;seed(db);prepare(db);build_orders(db,['card','second'],'other');admin()
    history=client.get('/api/v1/admin/user-directory/other/purchases?limit=1').json()
    assert history['total']==2 and len(history['items'])==1
    assert client.get('/api/v1/admin/sales/'+history['items'][0]['id']).status_code==200
    assert client.get('/api/v1/admin/user-directory/seller/messages').json()['total']==1
    assert client.get('/api/v1/admin/user-directory/missing').status_code==404
