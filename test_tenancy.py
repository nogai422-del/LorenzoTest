import asyncio
import importlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='lorenzo-tests-')
os.environ['OWNER_ID'] = '111'
os.environ['BOT_TOKEN'] = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
os.environ['WEB_ADMIN_PASSWORD'] = 'local-test-password'
os.environ['WEB_COOKIE_SECURE'] = '0'

import db
db.init_db()
import tenancy as t
t.init_tenancy()
import pytest


@pytest.fixture(autouse=True)
def clean():
    with db.get_conn() as c:
        for table in ['tenant_config','tenant_admins','tenant_tickets','tenant_orders','chats','chat_members','sync_requests']:
            c.execute('DELETE FROM '+table)


def grant(cid,level=2,uid=42):
    token,amount=t.create_order(cid,uid,level)
    t.confirm_payment(token,uid,amount,'XTR','charge-'+token)
    return token


def test_free_and_home():
    assert t.tier(-1001)==0
    assert t.tier(t.HOME_CHAT_ID)==2
    assert t.settings(-1001)['level']==2


def test_settings_do_not_cross():
    grant(-1001,1);grant(-1002,1)
    cfg=t.settings(-1001);cfg['texts']['welcome_text']='Only A'
    t.save_chat_settings(-1001,cfg)
    assert t.settings(-1001)['texts']['welcome_text']=='Only A'
    assert t.settings(-1002)['texts']['welcome_text']!='Only A'
    with pytest.raises(PermissionError):t.save_chat_settings(-1003,cfg)


def test_upgrade_and_replay():
    token=grant(-1001,1)
    row=t.get_order(token)
    assert not t.confirm_payment(token,42,row['amount'],'XTR',row['charge'])
    upgrade,amount=t.create_order(-1001,42,2)
    assert amount==t.star_price(2)-t.star_price(1)
    t.confirm_payment(upgrade,42,amount,'XTR','upgrade')
    assert t.tier(-1001)==2 and t.tier(-1002)==0


@pytest.mark.parametrize('uid,amount,currency',[(43,50,'XTR'),(42,999,'XTR'),(42,50,'RUB')])
def test_payment_rejects_forgery(uid,amount,currency):
    token,_=t.create_order(-1001,42,2)
    with pytest.raises(ValueError):t.confirm_payment(token,uid,amount,currency,'bad')
    assert t.tier(-1001)==0


def test_ticket_single_use():
    token=t.new_ticket(-1001,42)
    assert t.consume_ticket(token)['chat_id']==-1001
    assert t.consume_ticket(token) is None


def test_refund_restores_previous_level():
    grant(-1001,1)
    token=grant(-1001,2)
    t.record_refund(t.get_order(token)['charge'])
    assert t.tier(-1001)==1 and t.tier(t.HOME_CHAT_ID)==2


def test_telethon_never_falls_back_to_owner(monkeypatch):
    monkeypatch.setenv('TELETHON_API_ID','123')
    monkeypatch.setenv('TELETHON_API_HASH','owner-secret')
    monkeypatch.setenv('TELETHON_SESSION_STRING','owner-session')
    from telethon_config import load_telethon_config,save_telethon_config,config_path
    token=t.chat_context.set(-1002)
    try:
        assert not load_telethon_config()['session_string']
        assert not load_telethon_config()['api_hash']
        save_telethon_config(api_id=456,api_hash='tenant-secret',session_string='tenant-session')
        path=config_path()
    finally:t.chat_context.reset(token)
    token=t.chat_context.set(-1003)
    try:
        assert config_path()!=path
        assert not load_telethon_config()['session_string']
    finally:t.chat_context.reset(token)


def test_queue_scoped():
    db.request_sync(-1001,'test','full');db.request_sync(-1002,'test','full')
    assert {r['chat_id'] for r in db.claim_sync_requests(chat_id=-1001)}=={-1001}
    assert {r['chat_id'] for r in db.claim_sync_requests(chat_id=-1002)}=={-1002}


def test_telethon_rejects_other_chat(monkeypatch):
    from telethon_config import save_telethon_config
    from telethon.sessions import StringSession
    from telethon.crypto import AuthKey
    session=StringSession();session.set_dc(2,'149.154.167.51',443);session.auth_key=AuthKey(bytes(range(256)))
    token=t.chat_context.set(-1008)
    try:
        save_telethon_config(api_id=123,api_hash='a'*32,session_string=session.save(),enabled=True)
        from telethon_sync import TelethonSyncService
        svc=TelethonSyncService(-1008)
        with pytest.raises(ValueError):asyncio.run(svc.sync_chat(-1009))
    finally:t.chat_context.reset(token)


def test_removed_admin_fails_closed():
    bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),get_chat_administrators=AsyncMock(side_effect=RuntimeError()))
    assert not asyncio.run(t.is_chat_admin(bot,-1001,42))
    assert asyncio.run(t.refresh_admins(bot,-1001))==[]


def test_web_boundaries(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app
    import bot_app
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='administrator')))
    monkeypatch.setattr(bot_app.bot,'get_chat_administrators',AsyncMock(return_value=[]))
    grant(-1001);grant(-1002)
    db.record_chat(-1001,'Tenant A',None,'supergroup',1,tracked=True,discovered_by='bot')
    db.record_chat(-1002,'Secret Tenant B',None,'supergroup',1,tracked=True,discovered_by='bot')
    db.set_joined(-1001,70,'Alice',1)
    db.set_joined(-1002,80,'Secret Bob',1)
    client=TestClient(web_app.app)
    token=t.new_ticket(-1001,42)
    response=client.get('/tenant/login',params={'token':token})
    assert response.status_code==200 and 'Alice' in response.text and 'Secret Bob' not in response.text
    assert 'Secret Tenant B' not in response.text
    assert db.dashboard_stats(-1001)['tracked_chats']==1
    assert client.get('/tenant/-1002').status_code==403
    assert client.post('/tenant/-1002/settings',data={'csrf':'fake','welcome_text':'a','consent_text':'b','nudge_text':'c'}).status_code==403
    assert client.get('/creator',follow_redirects=False).status_code==303
    assert client.get('/members',follow_redirects=False).status_code==303
    assert client.post('/tenant/-1001/alerts',data={'csrf':'fake','days':3,'enabled':1}).status_code==403
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='member')))
    assert client.get('/tenant/-1001').status_code==403


def test_legacy_admin_cannot_access_creator():
    from fastapi.testclient import TestClient
    import web_app
    from web_auth import hash_password
    db.add_system_admin(43)
    db.set_admin_web_credentials(43,web_username='tenant-admin',password_hash=hash_password('test-password-123'))
    client=TestClient(web_app.app)
    response=client.post('/login',data={'username':'tenant-admin','password':'test-password-123'},follow_redirects=False)
    assert response.status_code==303
    assert client.get('/creator').status_code==403
    assert client.get('/members').status_code==403


def test_creator_metrics_and_preview(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app
    import bot_app
    grant(-1001)
    db.record_chat(-1001,'Example',None,'supergroup',1,tracked=True,discovered_by='bot')
    client=TestClient(web_app.app)
    client.post('/login',data={'username':'admin','password':'local-test-password'},follow_redirects=False)
    response=client.get('/creator')
    assert response.status_code==200 and 'Объявления' in response.text and '50 ⭐' in response.text
    import re
    csrf=re.search('name="csrf" value="([^"]+)"',response.text).group(1)
    send=AsyncMock();monkeypatch.setattr(bot_app.bot,'send_message',send)
    response=client.post('/creator/preview',data={'csrf':csrf,'chat_id':-1001,'text':'<test> Support Lorenzo'})
    assert response.status_code==200 and '&lt;test&gt;' in response.text
    send.assert_not_called()
    token=re.search('name="token" value="([^"]+)"',response.text).group(1)
    assert client.post('/creator/send',data={'csrf':csrf,'token':token}).status_code==200
    assert client.post('/creator/send',data={'csrf':csrf,'token':token}).status_code==400
    send.assert_called_once()


def test_creator_chat_link_is_owner_only(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app,bot_app
    db.record_chat(-1001,'Example',None,'supergroup',1,tracked=True,discovered_by='bot')
    client=TestClient(web_app.app)
    assert client.post('/creator/chat-link',data={'csrf':'bad','chat_id':-1001},follow_redirects=False).status_code==303
    client.post('/login',data={'username':'admin','password':'local-test-password'},follow_redirects=False)
    import re
    csrf=re.search('name="csrf" value="([^"]+)"',client.get('/creator').text).group(1)
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='administrator')))
    monkeypatch.setattr(bot_app.bot,'get_chat',AsyncMock(return_value=SimpleNamespace(username=None,invite_link=None,title='Example')))
    create=AsyncMock(return_value=SimpleNamespace(invite_link='https://t.me/+demo'))
    monkeypatch.setattr(bot_app.bot,'create_chat_invite_link',create)
    response=client.post('/creator/chat-link',data={'csrf':csrf,'chat_id':-1001})
    assert response.status_code==200 and 'https://t.me/+demo' in response.text
    assert create.call_args.kwargs['member_limit']==1
