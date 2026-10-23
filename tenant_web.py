from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import time
import html
from fastapi import Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import tenancy as t
from db import get_conn, get_chat_info, dashboard_stats, list_members_for_web, list_known_chats, request_sync, get_chat_settings, set_chat_settings


def install(app, owner_session, secret, secure):
    t.init_tenancy()
    previews = {}
    auth_managers = {}

    def sign(body):
        return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    def encode(value):
        import base64
        body = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return body+'.'+sign(body)

    def read(request):
        import base64
        try:
            body, sig = request.cookies.get('lorenzo_chat_session','').rsplit('.',1)
            if not hmac.compare_digest(sig,sign(body)): return None
            value = json.loads(base64.urlsafe_b64decode(body))
            return value if value['exp'] > time.time() else None
        except (ValueError,KeyError,TypeError): return None

    async def access(request, cid, csrf=None):
        from bot_app import bot
        session = read(request)
        if not session or session['cid'] != cid or t.tier(cid) < 2:
            raise HTTPException(403,'Нет доступа к чату')
        if csrf is not None and not hmac.compare_digest(session['csrf'], csrf):
            raise HTTPException(403,'Недействительный запрос')
        if not await t.is_chat_admin(bot,cid,session['uid']):
            raise HTTPException(403,'Права администратора отозваны')
        await t.refresh_admins(bot,cid)
        return session

    def page(title, content):
        # Escape all external values before composing HTML.
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+html.escape(title)+'</title><style>body{margin:0;background:#10131c;color:#e7ecf5;font:16px system-ui}main{max-width:1100px;margin:50px auto;padding:24px}'
            'h1{font-size:32px}h2{margin-top:32px}a{color:#a6c8ff}section{padding:24px;background:#1b2231;border-radius:16px;margin:18px 0}'
            'table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #343d50}'
            'input,textarea,select,button{font:inherit;padding:10px;background:#111827;color:#eee;border:1px solid #56617a;border-radius:8px;margin:5px 0;box-sizing:border-box}'
            'textarea{width:100%;min-height:110px}button{background:#4968cc;cursor:pointer}label{display:block;margin-top:14px}.stats{display:flex;gap:20px;flex-wrap:wrap}.stats section{flex:1;min-width:180px}'
            '.big{font-size:30px;font-weight:700}small{color:#aebbd1}pre{white-space:pre-wrap}</style><main><small>LORENZO</small><h1>'+html.escape(title)+'</h1>'+content+'</main></html>')

    def hidden(csrf):
        return '<input type="hidden" name="csrf" value="'+html.escape(csrf)+'">'

    @app.get('/tenant/login')
    async def login(request: Request, token: str):
        ticket = t.consume_ticket(token)
        if not ticket: raise HTTPException(403,'Ссылка истекла. Запросите новую у бота.')
        from bot_app import bot
        cid, uid = ticket['chat_id'],ticket['user_id']
        if t.tier(cid)<2 or not await t.is_chat_admin(bot,cid,uid): raise HTTPException(403,'Нет доступа')
        response=RedirectResponse(f'/tenant/{cid}',303)
        response.set_cookie('lorenzo_chat_session',encode(dict(cid=cid,uid=uid,exp=int(time.time())+3600,csrf=secrets.token_urlsafe(24))),
                            httponly=True,secure=secure,samesite='strict',max_age=3600,path='/tenant')
        return response

    @app.get('/tenant/{cid}')
    async def panel(request: Request,cid: int):
        session=await access(request,cid)
        info=get_chat_info(cid) or {}
        stats=dashboard_stats(cid)
        cfg=t.settings(cid)
        rows=list_members_for_web(cid,limit=200)
        with get_conn() as c:
            admins=c.execute('SELECT user_id FROM tenant_admins WHERE chat_id=?',(cid,)).fetchall()
        labels=dict(members='Участников',active_24h='Активных за сутки',active_7d='Активных за 7 дней',inactive_7d='Не писали 7 дней',never_wrote='Без сообщений',joined_7d='Вступили за 7 дней',left_7d='Вышли за 7 дней',tracked_chats='Чатов в панели')
        content='<p>Данные, настройки и Telethon относятся только к этому чату.</p>'
        content+='<div class="stats">'+''.join('<section>'+html.escape(labels.get(k,str(k)))+'<div class="big">'+html.escape(str(v))+'</div></section>' for k,v in stats.items() if isinstance(v,(int,float)))+'</div>'
        content+=f'<section><h2>Настройки онбординга</h2><form method="post" action="/tenant/{cid}/settings">'+hidden(session['csrf'])
        for key,label in [('welcome_text','Приветствие · {name}'),('consent_text','Анкета · {age}'),('nudge_text','Напоминание · {mention}')]:
            content+='<label>'+label+'</label><textarea name="'+key+'">'+html.escape(cfg['texts'][key])+'</textarea>'
        content+='<button>Сохранить настройки чата</button></form></section>'
        content+='<section><h2>Администраторы этого чата</h2><p>'+', '.join(str(a['user_id']) for a in admins)+'</p><small>Состав берётся из Telegram. Удаление администратора в Telegram отзывает доступ к панели.</small></section>'
        content+='<section><h2>Участники</h2><table><tr><th>Участник</th><th>Сообщений</th></tr>'
        for row in rows:
            content+='<tr><td>'+html.escape(row['user_name'] or str(row['user_id']))+'</td><td>'+str(row['total_message_count'])+'</td></tr>'
        content+='</table><small>Показаны первые 200 участников.</small></section>'
        content+=f'<section><h2>Свой Telethon</h2><p>Используйте API ID и API Hash собственного приложения Telegram. Сессия создателя бота для этого чата не используется.</p><form method="post" action="/tenant/{cid}/telethon/config">'+hidden(session['csrf'])+'<label>API ID</label><input name="api_id" type="number" required><label>API Hash</label><input name="api_hash" type="password" autocomplete="off" required><button>Сохранить</button></form>'
        token=t.chat_context.set(cid)
        try:
            from telethon_config import telethon_status
            status=telethon_status()
            content+='<p>Статус: '+html.escape(status['status_label'])+'</p>'
            manager=auth_managers.get(cid)
            if manager: content+='<p>Этап входа: '+html.escape(manager.step)+'</p>'
        finally:t.chat_context.reset(token)
        for action,label,name,kind in [('phone','Запросить код','phone','tel'),('code','Подтвердить код','code','text'),('password','Подтвердить 2FA','password','password')]:
            content+=f'<form method="post" action="/tenant/{cid}/telethon/{action}">'+hidden(session['csrf'])+f'<input name="value" type="{kind}" aria-label="{name}" placeholder="{name}" autocomplete="off" required><button>{label}</button></form>'
        content+=f'<form method="post" action="/tenant/{cid}/sync">'+hidden(session['csrf'])+'<button>Синхронизировать этот чат</button></form>'
        content+=f'<form method="post" action="/tenant/{cid}/telethon/disconnect">'+hidden(session['csrf'])+'<button>Отключить Telethon этого чата</button></form></section>'
        alert_cfg=get_chat_settings(cid)
        content+=f'<section><h2>Неактивность</h2><form method="post" action="/tenant/{cid}/alerts">'+hidden(session['csrf'])+f'<label>Дней без сообщений</label><input name="days" type="number" min="1" max="365" value="{alert_cfg["inactivity_days"]}"><label>Оповещения</label><select name="enabled"><option value="1">Включены</option><option value="0">Выключены</option></select><button>Сохранить</button></form></section>'
        return page(info.get('title') or str(cid),content)

    @app.post('/tenant/{cid}/settings')
    async def save(request: Request,cid: int,csrf: str=Form(...),welcome_text: str=Form(...),consent_text: str=Form(...),nudge_text: str=Form(...)):
        await access(request,cid,csrf)
        import string
        for text,allowed in [(welcome_text,{'name'}),(consent_text,{'age'}),(nudge_text,{'mention','name','username','user_id'})]:
            try:
                if len(text)>1500 or any(f and f not in allowed for _,f,_,_ in string.Formatter().parse(text)): raise ValueError()
            except ValueError: raise HTTPException(400,'Недопустимый шаблон')
        cfg=t.settings(cid);cfg['texts']=dict(welcome_text=welcome_text,consent_text=consent_text,nudge_text=nudge_text)
        t.save_chat_settings(cid,cfg)
        return RedirectResponse(f'/tenant/{cid}',303)

    @app.post('/tenant/{cid}/alerts')
    async def alerts(request: Request,cid: int,csrf: str=Form(...),days: int=Form(...),enabled: int=Form(...)):
        await access(request,cid,csrf)
        if not 1<=days<=365 or enabled not in (0,1): raise HTTPException(400,'Некорректные настройки')
        set_chat_settings(cid,inactivity_days=days,enabled=bool(enabled))
        return RedirectResponse(f'/tenant/{cid}',303)

    @app.post('/tenant/{cid}/sync')
    async def sync(request: Request,cid: int,csrf: str=Form(...)):
        session=await access(request,cid,csrf)
        request_sync(cid,str(session['uid']),'full')
        return RedirectResponse(f'/tenant/{cid}',303)

    @app.post('/tenant/{cid}/telethon/config')
    async def config(request: Request,cid: int,csrf: str=Form(...),api_id: int=Form(...),api_hash: str=Form(...)):
        await access(request,cid,csrf)
        token=t.chat_context.set(cid)
        try:
            from telethon_config import save_telethon_config
            import re
            if api_id<=0 or not re.fullmatch('[a-fA-F0-9]{32}',api_hash): raise HTTPException(400,'Проверьте API ID и Hash')
            manager=auth_managers.pop(cid,None)
            if manager: await manager.reset(clear_pending=True)
            save_telethon_config(api_id=api_id,api_hash=api_hash,enabled=True,session_string='',account={})
        finally:t.chat_context.reset(token)
        return RedirectResponse(f'/tenant/{cid}',303)

    @app.post('/tenant/{cid}/telethon/{action}')
    async def auth(request: Request,cid: int,action: str,csrf: str=Form(...),value: str=Form('')):
        await access(request,cid,csrf)
        from telethon_auth import TelethonAuthManager,TelethonAuthError
        from telethon_config import save_telethon_config
        token=t.chat_context.set(cid)
        try:
            manager=auth_managers.setdefault(cid,TelethonAuthManager())
            if action=='phone':await manager.send_code(value)
            elif action=='code':await manager.submit_code(value)
            elif action=='password':await manager.submit_password(value)
            elif action=='disconnect':
                await manager.reset(clear_pending=True)
                save_telethon_config(enabled=False,session_string='',account={})
            else:raise HTTPException(404)
        except TelethonAuthError as exc:raise HTTPException(400,str(exc))
        finally:t.chat_context.reset(token)
        return RedirectResponse(f'/tenant/{cid}',303)

    @app.get('/creator')
    async def creator(request: Request):
        session=owner_session(request)
        from datetime import datetime,timezone,timedelta
        month_start=int(datetime.now(timezone(timedelta(hours=3))).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())
        with get_conn() as c:
            revenue=c.execute('SELECT COALESCE(SUM(amount),0) n,COUNT(*) cnt FROM tenant_orders WHERE paid IS NOT NULL AND refunded=0').fetchone()
            month=c.execute("SELECT COALESCE(SUM(amount),0) n FROM tenant_orders WHERE paid>=? AND refunded=0",(month_start,)).fetchone()
            reach=c.execute('SELECT COUNT(DISTINCT user_id) n FROM chat_members WHERE is_bot=0 AND is_active=1').fetchone()['n']
            payments=c.execute('SELECT chat_id,amount,tier,paid FROM tenant_orders WHERE paid IS NOT NULL ORDER BY paid DESC LIMIT 50').fetchall()
        chats=list_known_chats(include_untracked=True)
        content='<p><a href="/">Управление сервисом</a></p><div class="stats">'
        for label,value in [('Доход за всё время',str(revenue['n'])+' ⭐'),('За месяц · МСК',str(month['n'])+' ⭐'),('Чатов',len(chats)),('Уникальных участников',reach)]:
            content+='<section>'+label+'<div class="big">'+str(value)+'</div></section>'
        content+='</div><small>Доход — подтверждённые платежи боту в Stars, до комиссий и вывода. Охват — известные активные участники; полнота зависит от синхронизации.</small>'
        content+='<section><h2>Подключённые чаты</h2><table><tr><th>Чат</th><th>ID</th><th>Тариф</th><th>Открыть</th></tr>'
        options=''
        for chat in chats:
            cid=int(chat['chat_id']);name=html.escape(chat['title'] or str(cid))
            content+=f'<tr><td>{name}</td><td>{cid}</td><td>{t.tier(cid)}</td><td><form method="post" action="/creator/chat-link">'+hidden(session['csrf'])+f'<input type="hidden" name="chat_id" value="{cid}"><button>Войти в чат</button></form></td></tr>'
            if chat['discovered_by']=='bot':options+=f'<option value="{cid}">{name}</option>'
        content+='</table></section><section><h2>Объявления</h2><p>Выберите чат и подготовьте короткое сообщение о боте или донате.</p><form action="/creator/preview" method="post">'+hidden(session['csrf'])+'<select name="chat_id">'+options+'</select><textarea name="text" maxlength="1000" placeholder="Текст объявления" required></textarea><button>Предпросмотр</button></form></section>'
        content+='<section><h2>Последние платежи</h2><table><tr><th>Чат</th><th>Stars</th><th>Уровень</th></tr>'+''.join(f'<tr><td>{r["chat_id"]}</td><td>{r["amount"]}</td><td>{r["tier"]}</td></tr>' for r in payments)+'</table></section>'
        return page('Панель создателя',content)

    @app.post('/creator/chat-link')
    async def chat_link(request: Request,csrf: str=Form(...),chat_id: int=Form(...)):
        session=owner_session(request)
        if not hmac.compare_digest(session['csrf'],csrf):raise HTTPException(403)
        if not get_chat_info(chat_id):raise HTTPException(404,'Чат не найден')
        from bot_app import bot
        from db import audit
        try:
            member=await bot.get_chat_member(chat_id,bot.id)
            if member.status in {'left','kicked'}:raise HTTPException(400,'Бот больше не состоит в чате')
            info=await bot.get_chat(chat_id)
            if info.username:
                url='https://t.me/'+info.username
            elif info.invite_link:
                url=info.invite_link
            else:
                link=await bot.create_chat_invite_link(chat_id,name='Lorenzo creator',expire_date=int(time.time())+3600,member_limit=1)
                url=link.invite_link
            audit('creator:'+str(session['uid']),'chat_link_requested',str(chat_id))
            return page('Войти в чат','<section><h2>'+html.escape(info.title or str(chat_id))+'</h2><a rel="noreferrer" href="'+html.escape(url,quote=True)+'">Открыть чат в Telegram</a><p>Если создана новая ссылка, она действует один час и рассчитана на один вход.</p></section><a href="/creator">Вернуться</a>')
        except HTTPException:raise
        except Exception:
            return page('Ссылка недоступна','<p>Для закрытого чата боту нужны права администратора с возможностью приглашать участников. Получите ссылку у администратора чата.</p><a href="/creator">Вернуться</a>')

    @app.post('/creator/preview')
    async def preview(request: Request,csrf: str=Form(...),chat_id: int=Form(...),text: str=Form(...)):
        session=owner_session(request)
        if not hmac.compare_digest(session['csrf'],csrf):raise HTTPException(403)
        info=get_chat_info(chat_id)
        if not info or info['discovered_by']!='bot' or not 1<=len(text.strip())<=1000:raise HTTPException(400,'Проверьте чат и текст')
        token=secrets.token_urlsafe(24)
        previews[token]=(chat_id,text,int(time.time())+600,session['uid'])
        return page('Предпросмотр объявления','<section><h2>'+html.escape(info['title'] or str(chat_id))+'</h2><pre>'+html.escape(text)+'</pre><form method="post" action="/creator/send">'+hidden(csrf)+'<input type="hidden" name="token" value="'+token+'"><button>Отправить в этот чат</button></form></section>')

    @app.post('/creator/send')
    async def send(request: Request,csrf: str=Form(...),token: str=Form(...)):
        session=owner_session(request)
        if not hmac.compare_digest(session['csrf'],csrf):raise HTTPException(403)
        item=previews.pop(token,None)
        if not item or item[2]<time.time() or item[3]!=session['uid']:raise HTTPException(400,'Предпросмотр истёк')
        from bot_app import bot
        from db import audit
        await bot.send_message(item[0],item[1],parse_mode=None,disable_web_page_preview=True)
        audit('creator:'+str(session['uid']),'announcement_sent',str(item[0]))
        return page('Объявление отправлено','<p>Сообщение отправлено в выбранный чат.</p><a href="/creator">Вернуться</a>')
