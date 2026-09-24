import asyncio
from sqlalchemy import select
from test_community import community, start
from alerts import Notification, record_notifications
from notifications import send_push
import main


def test_unread_messages_are_private_and_read_cursor_is_monotonic(community):
    client, db, actor, _ = community
    chat = start(client)
    client.post(f'/api/v1/messages/{chat}', json={'body': 'First', 'client_id': 'first'})
    assert client.get('/api/v1/activity/unread').json()['messages'] == 0
    actor['name'] = 'seller'
    state = client.get('/api/v1/activity/unread').json()
    assert state['messages'] == 1
    first = state['latest_message_id']
    assert client.post(f'/api/v1/messages/{chat}/read', json={'through_id':first}).status_code == 200
    assert client.get('/api/v1/activity/unread').json()['messages'] == 0
    actor['name'] = 'buyer'
    client.post(f'/api/v1/messages/{chat}', json={'body':'Second','client_id':'second'})
    actor['name'] = 'seller'
    client.post(f'/api/v1/messages/{chat}/read', json={'through_id':0})
    assert client.get('/api/v1/activity/unread').json()['messages'] == 1
    actor['name'] = 'stranger'
    assert client.get('/api/v1/activity/unread').json()['messages'] == 0
    assert client.post(f'/api/v1/messages/{chat}/read', json={'through_id':999}).status_code == 404


def test_notifications_persist_without_firebase_and_scope_read_markers(community):
    client, db, actor, _ = community
    asyncio.run(send_push(main.settings, db, ['buyer','buyer','seller'], 'Shipped', 'Your card shipped'))
    rows = client.get('/api/v1/notifications').json()
    assert len(rows) == 1
    first = rows[0]['id']
    assert client.get('/api/v1/activity/unread').json()['notifications'] == 1
    record_notifications(db, ['buyer'], 'New', 'Later', {})
    client.post('/api/v1/notifications/read', json={'through_id':first})
    assert client.get('/api/v1/activity/unread').json()['notifications'] == 1
    record_notifications(db, ['buyer'], 'Chat', 'Hello', {'conversation_id':'chat'})
    assert len(client.get('/api/v1/notifications').json()) == 2
    actor['name'] = 'seller'
    assert client.post(f'/api/v1/notifications/{first}/read').status_code == 404
    assert client.get('/api/v1/activity/unread').json()['notifications'] == 1
    actor['name'] = 'stranger'
    assert client.get('/api/v1/notifications').json() == []
