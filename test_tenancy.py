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
def clean(monkeypatch):
    import bot_app
    import bot_permissions
    bot_permissions._status.clear()
    bot_permissions._notices.clear()
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='administrator')))
    monkeypatch.setattr(bot_app.bot,'get_chat_administrators',AsyncMock(return_value=[]))
    monkeypatch.setattr(bot_app.bot,'get_chat',AsyncMock(return_value=SimpleNamespace(permissions=None)))
    with db.get_conn() as c:
        for table in ['tenant_credentials','tenant_config','tenant_admins','tenant_tickets','tenant_orders','chats','chat_members','sync_requests']:
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
    login,password=t.issue_credentials(-1001,42)
    response=tenant_login(client,login,password)
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


def tenant_login(client,login,password):
    import re
    response=client.get('/tenant/login')
    csrf=re.search('name="csrf" value="([^"]+)"',response.text).group(1)
    return client.post('/tenant/login',data={'username':login,'password':password,'csrf':csrf})


def test_passwords_unique_and_hashed():
    grant(-1001);grant(-1002)
    login1,pw1=t.issue_credentials(-1001,42)
    login2,pw2=t.issue_credentials(-1002,42)
    assert login1!=login2 and pw1!=pw2
    row=t.credential_for_login(login1)
    assert row['password_hash']!=pw1 and row['chat_id']==-1001
    from web_auth import verify_password
    assert verify_password(pw1,row['password_hash'])
    assert not verify_password(pw2,row['password_hash'])


def test_password_rotation_revokes_sessions(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app,bot_app
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='administrator')))
    monkeypatch.setattr(bot_app.bot,'get_chat_administrators',AsyncMock(return_value=[]))
    grant(-1001)
    login,pw=t.issue_credentials(-1001,42)
    client=TestClient(web_app.app)
    assert tenant_login(client,login,pw).status_code==200
    login2,pw2=t.issue_credentials(-1001,42)
    assert login2==login and pw2!=pw
    assert client.get('/tenant/-1001').status_code==403
    assert tenant_login(client,login,pw).status_code==401
    assert tenant_login(client,login2,pw2).status_code==200


def test_password_login_csrf_and_removed_admin(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app,bot_app
    grant(-1001)
    login,pw=t.issue_credentials(-1001,42)
    client=TestClient(web_app.app)
    assert client.post('/tenant/login',data={'username':login,'password':pw,'csrf':'fake'}).status_code==403
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(return_value=SimpleNamespace(status='member')))
    assert tenant_login(client,login,pw).status_code==401
    token=t.new_ticket(-1001,42)
    response=client.get('/tenant/login',params={'token':token})
    assert 'Вход по старым' in response.text
    assert client.get('/tenant/-1001').status_code==403


def test_credentials_are_per_admin():
    grant(-1001)
    first,pw=t.issue_credentials(-1001,42)
    second,pw2=t.issue_credentials(-1001,43)
    old=t.credential_version(-1001,43)
    t.issue_credentials(-1001,42)
    assert first!=second and pw!=pw2
    assert t.credential_version(-1001,43)==old


def test_announcement_chat_not_found(monkeypatch):
    from fastapi.testclient import TestClient
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChatMember
    import web_app,bot_app,re
    db.record_chat(-1001,'Unavailable second chat',None,'supergroup',1,tracked=True,discovered_by='bot')
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(side_effect=TelegramBadRequest(method=GetChatMember(chat_id=-1001,user_id=bot_app.bot.id),message='Bad Request: chat not found')))
    send=AsyncMock();monkeypatch.setattr(bot_app.bot,'send_message',send)
    client=TestClient(web_app.app)
    client.post('/login',data={'username':'admin','password':'local-test-password'},follow_redirects=False)
    response=client.get('/creator')
    assert response.status_code==200 and 'нет доступа бота' in response.text
    csrf=re.search('name="csrf" value="([^"]+)"',response.text).group(1)
    response=client.post('/creator/preview',data={'csrf':csrf,'chat_id':-1001,'text':'Announcement'})
    assert response.status_code==200 and 'Чат недоступен самому Лоренцо' in response.text
    send.assert_not_called()


def test_announcement_send_error_is_readable(monkeypatch):
    from fastapi.testclient import TestClient
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    import web_app,bot_app,re
    db.record_chat(-1001,'Example',None,'supergroup',1,tracked=True,discovered_by='telethon')
    client=TestClient(web_app.app)
    client.post('/login',data={'username':'admin','password':'local-test-password'},follow_redirects=False)
    response=client.get('/creator')
    csrf=re.search('name="csrf" value="([^"]+)"',response.text).group(1)
    response=client.post('/creator/preview',data={'csrf':csrf,'chat_id':-1001,'text':'Announcement'})
    assert 'Предпросмотр объявления' in response.text
    token=re.search('name="token" value="([^"]+)"',response.text).group(1)
    monkeypatch.setattr(bot_app.bot,'send_message',AsyncMock(side_effect=TelegramBadRequest(method=SendMessage(chat_id=-1001,text='x'),message='Bad Request: chat not found')))
    response=client.post('/creator/send',data={'csrf':csrf,'token':token})
    assert response.status_code==200 and 'Сообщение не отправлено' in response.text and 'Чат недоступен самому' in response.text


def test_restricted_announcement_bot():
    from announcements import destination_status
    bot=SimpleNamespace(id=123,get_chat_member=AsyncMock(return_value=SimpleNamespace(status='restricted',can_send_messages=False)))
    available,reason=asyncio.run(destination_status(bot,-1001))
    assert not available and 'нет разрешения' in reason


def test_non_admin_bot_does_not_process_group_message():
    from tenant_bot import ChatBoundaryMiddleware
    from aiogram.types import Message,Chat,User
    from datetime import datetime
    bot=SimpleNamespace(id=123,get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),send_message=AsyncMock())
    message=Message(message_id=1,date=datetime.now(),chat=Chat(id=-1001,type='supergroup'),from_user=User(id=42,is_bot=False,first_name='Admin'),text='/admin').as_(bot)
    handler=AsyncMock()
    asyncio.run(ChatBoundaryMiddleware()(handler,message,{}))
    handler.assert_not_called()
    assert 'приостановлен' in bot.send_message.call_args.args[1]


def test_demotion_invalidates_cached_admin_status():
    from bot_permissions import has_admin_rights,update_status
    bot=SimpleNamespace(id=123,get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')))
    assert asyncio.run(has_admin_rights(bot,-1001))
    update_status(-1001,'member')
    assert not asyncio.run(has_admin_rights(bot,-1001))


def test_admin_warning_throttled():
    from bot_permissions import warn
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(warn(bot,-1001))
    asyncio.run(warn(bot,-1001))
    bot.send_message.assert_called_once()


def test_admin_cannot_login_when_bot_is_only_member(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app,bot_app
    grant(-1001)
    login,pw=t.issue_credentials(-1001,42)
    monkeypatch.setattr(bot_app.bot,'get_chat_member',AsyncMock(side_effect=lambda cid,uid:SimpleNamespace(status='member' if uid==bot_app.bot.id else 'administrator')))
    assert tenant_login(TestClient(web_app.app),login,pw).status_code==401
