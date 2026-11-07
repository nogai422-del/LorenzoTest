"""Chat-local participants, bot administrators and notification menus."""
from __future__ import annotations
import html
import secrets
import time
from aiogram import Router,F
from aiogram.filters import StateFilter,Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import InlineKeyboardMarkup,ForceReply
from aiogram.dispatcher.event.bases import UNHANDLED
import tenancy as t
from telegram_ui import render,clear_prompt,input_error
from db import get_conn,dashboard_stats,list_members_for_web,list_inactive_members_for_web,get_chat_member_stats,request_sync,audit
from chat_settings_tg import permitted,title

router=Router(name='chat_local_tools')


class Input(StatesGroup):
    value=State()
    confirm=State()
    results=State()


def b(cid,label,action):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=label,callback_data=f'tool:{cid}:{action}')


async def show(message,cid,text,rows,replace=True):
    rows.append([b(cid,'‹ Участники и управление','home')])
    await render(message,f'<b>{title(cid)}</b>\n\n'+text,InlineKeyboardMarkup(inline_keyboard=rows),replace=replace)


async def tools_home(message,cid,replace=True):
    rows=[[b(cid,'👥 Участники','members:active:0'),b(cid,'📊 Статистика','stats')],
          [b(cid,'🔎 Найти участника','search'),b(cid,'🤐 Не писали','members:silent:0')],
          [b(cid,'⚠️ Неактивные','members:inactive:0'),b(cid,'🚪 Вышедшие','members:left:0')],
          [b(cid,'👮 Администраторы Лоренцо','admins')],
          [b(cid,'🔔 Получатели оповещений','notifs')],
          [b(cid,'🔄 Синхронизация','sync')]]
    from aiogram.types import InlineKeyboardButton
    rows.append([InlineKeyboardButton(text='‹ Настройки чата',callback_data=f'cfg:{cid}:home')])
    await render(message,f'<b>{title(cid)}</b>\n\nУчастники, статистика, администраторы и уведомления — только этого чата.',InlineKeyboardMarkup(inline_keyboard=rows),replace=replace)


async def members(message,cid,kind='active',page=0,search='',replace=True):
    page=max(0,min(10000,page));size=8
    if kind=='inactive':
        allrows=list_inactive_members_for_web(cid,limit=500)
        probe=allrows[page*size:page*size+size+1]
    else:probe=list_members_for_web(cid,active=kind,search=search,limit=size+1,offset=page*size)
    rows=probe[:size]
    labels={'active':'Участники','silent':'Ни разу не писали','inactive':'Неактивные','left':'Вышедшие'}
    text=f'<b>{labels.get(kind,"Участники")}</b> · страница {page+1}\n'
    if search:text+='Поиск: '+html.escape(search[:100])+'\n'
    buttons=[]
    for n,row in enumerate(rows,page*size+1):
        uid=int(row['user_id']);name=html.escape(row['user_name'] or str(uid))
        text+=f'\n{n}. <a href="tg://user?id={uid}">{name}</a> · 7 дней: {row["messages_7d"]}, 30 дней: {row["messages_30d"]}'
        buttons.append([b(cid,'👤 '+str(row['user_name'] or uid)[:35],f'member:{uid}')])
    if not rows:text+='\nУчастников по этим условиям нет. Если данные ещё не загружены, запустите синхронизацию со своим Telethon.'
    if kind=='inactive':text+='\n\nПоказаны первые 500 подходящих участников.'
    nav=[]
    if page:nav.append(b(cid,'‹',f'searchpage:{page-1}' if search else f'members:{kind}:{page-1}'))
    if len(probe)>size:nav.append(b(cid,'›',f'searchpage:{page+1}' if search else f'members:{kind}:{page+1}'))
    if nav:buttons.append(nav)
    await show(message,cid,text,buttons,replace)


async def admins(message,cid,actor,page=0):
    tgrows=await message.bot.get_chat_administrators(cid)
    manager=await t.can_manage_operators(message.bot,cid,actor)
    entries=[(m.user.id,m.user.full_name,'Telegram',False) for m in tgrows if not m.user.is_bot]
    entries += [(r['user_id'],r['display_name'] or str(r['user_id']),'Лоренцо',True) for r in t.list_operators(cid)]
    page=max(0,min(10000,page));size=15
    text=f'<b>Администраторы Лоренцо</b> · страница {page+1}\n\n'
    buttons=[]
    for uid,name,source,removable in entries[page*size:(page+1)*size]:
        text+='• '+html.escape(name[:60])+f' <code>{uid}</code> · {source}\n'
        if manager and removable:buttons.append([b(cid,'Отозвать: '+name[:28],f'remove:{uid}')])
    if not entries:text+='Назначений пока нет.\n'
    text+='\nTelegram — доступ по правам в группе; Лоренцо — назначение только для бота. Назначения здесь не дают прав администратора Telegram. Управлять составом могут владелец группы, Telegram-админ с правом назначать админов и создатель бота, если он администратор этой группы.'
    nav=[]
    if page:nav.append(b(cid,'‹',f'admins:{page-1}'))
    if len(entries)>(page+1)*size:nav.append(b(cid,'›',f'admins:{page+1}'))
    if nav:buttons.append(nav)
    if manager:buttons.append([b(cid,'➕ Назначить администратора Лоренцо','add')])
    await show(message,cid,text,buttons)


async def confirmation(call,state,cid,operation,uid,label):
    token=secrets.token_hex(5)
    await state.clear();await state.set_state(Input.confirm)
    await state.update_data(tool_chat_id=cid,operation=operation,target_id=uid,token=token,started_at=int(time.time()),target_name=label)
    if operation=='grant':
        text='Назначить '+html.escape(label)+' администратором Лоренцо только в этом чате? Он получит управление настройками и участниками, а также возможность получить пароль к веб-панели чата. Права Telegram-группы не изменятся.'
    elif operation=='remove':text='Отозвать у '+html.escape(label)+' доступ к Лоренцо в этом чате? Его пароль и веб-сеансы этого чата будут отозваны.'
    else:text='Отправить участнику '+html.escape(label)+' напоминание непосредственно в эту группу?'
    await show(call.message,cid,text,[[b(cid,'✅ Подтвердить','apply:'+token),b(cid,'Отмена','home')]])


@router.callback_query(F.data.startswith('tool:'))
async def action(call,state:FSMContext):
    try:_,raw,action=call.data.split(':',2);cid=int(raw)
    except (ValueError,AttributeError):await call.answer('Некорректная кнопка',show_alert=True);return
    if not call.message or not await permitted(call.bot,cid,call.from_user.id,call.message.chat.id,call.message.chat.type,2):
        await call.answer('Нет доступа к этому чату или нужен полный тариф.',show_alert=True);return
    if action.startswith('apply:'):
        data=await state.get_data()
        if data.get('tool_chat_id')!=cid or data.get('token')!=action[6:] or time.time()-data.get('started_at',0)>300:
            await call.answer('Подтверждение устарело. Откройте меню заново.',show_alert=True);return
        uid=data['target_id'];operation=data['operation']
        await state.clear()
        if operation in {'grant','remove'}:
            if not await t.can_manage_operators(call.bot,cid,call.from_user.id):
                await call.answer('Недостаточно прав управлять администраторами.',show_alert=True);return
            try:target=await call.bot.get_chat_member(cid,uid)
            except Exception:
                if operation=='grant':
                    await call.answer('Telegram не подтвердил участие пользователя.',show_alert=True);return
                target=None
            if operation=='grant' and (not t.present_member(target) or target.user.is_bot):
                await call.answer('Пользователь должен состоять в этом чате.',show_alert=True);return
            if target and target.status in {'administrator','creator'}:
                await call.answer('У этого пользователя уже есть доступ как у администратора Telegram.',show_alert=True);return
            t.set_operator(cid,uid,call.from_user.id,data['target_name'],operation=='grant')
            audit('tg:'+str(call.from_user.id),'chat_operator_'+operation,f'{cid}:{uid}')
            await admins(call.message,cid,call.from_user.id)
        else:
            from bot_app import send_member_nudge
            try:await send_member_nudge(cid,uid,'tg:'+str(call.from_user.id))
            except Exception:
                await call.answer('Не удалось отправить напоминание. Проверьте состав и права бота.',show_alert=True);return
            await tools_home(call.message,cid)
        await call.answer('Готово');return
    if action.startswith('searchpage:'):
        data=await state.get_data()
        if data.get('tool_chat_id')!=cid or data.get('operation')!='search_results' or time.time()-data.get('started_at',0)>900:
            await call.answer('Поиск устарел. Выполните его заново.',show_alert=True);return
        try:page=int(action[11:])
        except ValueError:await call.answer('Некорректная страница',show_alert=True);return
        await members(call.message,cid,page=page,search=data['search'])
        await call.answer();return
    await clear_prompt(call.message,await state.get_data())
    await state.clear()
    if action=='home':await tools_home(call.message,cid)
    elif action=='stats':
        stats=dashboard_stats(cid)
        labels={'members':'Участников','active_24h':'Активны за сутки','active_7d':'Активны за 7 дней','inactive_7d':'Неактивны 7 дней','never_wrote':'Ни разу не писали','joined_7d':'Вступили за 7 дней','left_7d':'Вышли за 7 дней'}
        await show(call.message,cid,'📊 <b>Статистика чата</b>\n\n'+'\n'.join(f'{label}: <b>{stats[key]}</b>' for key,label in labels.items()),[])
    elif action.startswith('members:'):
        try:_,kind,p=action.split(':');page=int(p)
        except ValueError:await call.answer('Некорректная страница',show_alert=True);return
        if kind not in {'active','silent','inactive','left'}:await call.answer('Некорректный список',show_alert=True);return
        await members(call.message,cid,kind,page)
    elif action.startswith('member:'):
        try:uid=int(action[7:])
        except ValueError:await call.answer('Некорректный участник',show_alert=True);return
        row=get_chat_member_stats(cid,uid)
        if not row:await call.answer('Участник не найден в выбранном чате',show_alert=True);return
        name=html.escape(row.get('user_name') or str(uid))
        text=f'👤 <b>{name}</b>\nID: <code>{uid}</code>\nСообщений в этом чате: {row.get("total_message_count",0)}\nСтатус: '+('в группе' if row.get('is_active') else 'вышел из группы')
        rows=[[b(cid,'💬 Напомнить в группе',f'ping:{uid}')]] if row.get('is_active') and not row.get('is_bot') else []
        await show(call.message,cid,text,rows)
    elif action.startswith('ping:'):
        try:uid=int(action[5:])
        except ValueError:await call.answer('Некорректный участник',show_alert=True);return
        row=get_chat_member_stats(cid,uid)
        if not row or not row.get('is_active'):await call.answer('Участник не состоит в выбранном чате',show_alert=True);return
        await confirmation(call,state,cid,'ping',uid,row.get('user_name') or str(uid))
    elif action=='sync':
        token=t.chat_context.set(cid)
        try:
            from telethon_config import telethon_status
            configured=telethon_status()['authorized']
        finally:t.chat_context.reset(token)
        if not configured:await call.answer('Сначала подключите собственный Telethon для этого чата в персональной веб-панели.',show_alert=True);return
        request_sync(cid,'tg:'+str(call.from_user.id),'full')
        await call.answer('Синхронизация этого чата поставлена в очередь.',show_alert=True);return
    elif action=='admins' or action.startswith('admins:'):
        try:page=int(action.split(':')[1]) if ':' in action else 0
        except ValueError:await call.answer('Некорректная страница',show_alert=True);return
        await admins(call.message,cid,call.from_user.id,page)
    elif action in {'search','add'}:
        if action=='add' and not await t.can_manage_operators(call.bot,cid,call.from_user.id):
            await call.answer('Недостаточно прав управлять администраторами.',show_alert=True);return
        instruction='Введите Telegram ID или @username участника именно этого чата. ID можно узнать командой /myid. Поиск @username работает по известным участникам выбранного чата.' if action=='add' else 'Введите имя, @username или Telegram ID участника для поиска в этом чате.'
        prompt=await call.message.answer(f'<b>{title(cid)}</b>\n\n{instruction}\nОтветьте на это сообщение. /cancel — отмена.',reply_markup=ForceReply(selective=True))
        await state.set_state(Input.value)
        await state.update_data(tool_chat_id=cid,operation=action,prompt_id=prompt.message_id,started_at=int(time.time()))
    elif action.startswith('remove:'):
        if not await t.can_manage_operators(call.bot,cid,call.from_user.id):
            await call.answer('Недостаточно прав управлять администраторами.',show_alert=True);return
        try:uid=int(action[7:])
        except ValueError:await call.answer('Некорректный участник',show_alert=True);return
        row=next((r for r in t.list_operators(cid) if r['user_id']==uid),None)
        if not row:await call.answer('Назначение не найдено в этом чате',show_alert=True);return
        await confirmation(call,state,cid,'remove',uid,row['display_name'] or str(uid))
    elif action=='notifs':
        ids=await t.refresh_admins(call.bot,cid)
        text='<b>Уведомления администраторов этого чата</b>\n\n';rows=[]
        manager=await t.can_manage_operators(call.bot,cid,call.from_user.id)
        for uid in ids:
            text+=('🔔' if t.notifications_enabled(cid,uid) else '🔕')+f' <code>{uid}</code>\n'
            if uid==call.from_user.id or manager:rows.append([b(cid,('Выключить для ' if t.notifications_enabled(cid,uid) else 'Включить для ')+str(uid),f'notify:{uid}:{0 if t.notifications_enabled(cid,uid) else 1}')])
        rows.append([b(cid,'🧪 Тест оповещения мне','test')])
        await show(call.message,cid,text,rows)
    elif action.startswith('notify:'):
        try:_,rawuid,rawenabled=action.split(':');uid=int(rawuid);enabled=int(rawenabled)
        except ValueError:await call.answer('Некорректная кнопка',show_alert=True);return
        if enabled not in (0,1) or uid not in await t.refresh_admins(call.bot,cid) or (uid!=call.from_user.id and not await t.can_manage_operators(call.bot,cid,call.from_user.id)):
            await call.answer('Нет доступа к этой настройке',show_alert=True);return
        t.set_chat_notifications(cid,uid,bool(enabled))
        await tools_home(call.message,cid)
    elif action=='test':
        from bot_app import send_test_alert
        try:await send_test_alert(call.from_user.id,cid,'tg:'+str(call.from_user.id))
        except Exception:await call.answer('Сначала откройте личный чат с Лоренцо и нажмите Start.',show_alert=True);return
        await call.answer('Тест отправлен вам в личный чат.',show_alert=True);return
    else:await call.answer('Эта кнопка устарела. Откройте /admin.',show_alert=True);return
    await call.answer()


@router.message(StateFilter(Input.value,Input.confirm,Input.results),Command('cancel'))
async def cancel(message,state:FSMContext):
    data=await state.get_data()
    await state.clear()
    cid=data.get('tool_chat_id')
    if cid and await permitted(message.bot,cid,message.from_user.id,message.chat.id,message.chat.type,2):await tools_home(message,cid,replace=False)
    await clear_prompt(message,data,include_input=True)


@router.message(StateFilter(Input.value),F.text & ~F.text.startswith('/'))
async def value(message,state:FSMContext):
    data=await state.get_data();cid=data.get('tool_chat_id');operation=data.get('operation')
    if not cid or time.time()-data.get('started_at',0)>900:
        await state.clear();await message.answer('Время ввода истекло. Откройте /admin.');return
    if message.chat.type!='private' and (not message.reply_to_message or message.reply_to_message.message_id!=data.get('prompt_id')):return UNHANDLED
    if not await permitted(message.bot,cid,message.from_user.id,message.chat.id,message.chat.type,2):
        await state.clear();await message.answer('Доступ к этому чату отозван.');return
    text=message.text.strip()[:100]
    if operation=='search':
        await state.set_state(Input.results)
        await state.set_data(dict(tool_chat_id=cid,operation='search_results',search=text.lstrip('@'),started_at=int(time.time())))
        await members(message,cid,search=text.lstrip('@'),replace=False)
        await clear_prompt(message,data,include_input=True);return
    if operation!='add' or not await t.can_manage_operators(message.bot,cid,message.from_user.id):
        await state.clear();await message.answer('Нет прав управлять администраторами этого чата.');return
    uid=None
    if text.isdigit():uid=int(text)
    elif text.startswith('@'):
        with get_conn() as c:row=c.execute('SELECT user_id FROM chat_members WHERE chat_id=? AND LOWER(username)=LOWER(?) AND is_active=1',(cid,text[1:])).fetchone()
        if row:uid=int(row['user_id'])
    if not uid or uid<=0:
        await input_error(message,'Участник не найден в этом чате. Укажите его числовой Telegram ID.',f'tool:{cid}:home');return
    try:target=await message.bot.get_chat_member(cid,uid)
    except Exception:
        await input_error(message,'Telegram не подтвердил участие пользователя в выбранном чате.',f'tool:{cid}:home');return
    if not t.present_member(target) or target.user.is_bot:
        await input_error(message,'Назначить можно только человека, состоящего в выбранном чате.',f'tool:{cid}:home');return
    if target.status in {'creator','administrator'}:
        await state.clear();await message.answer('Этот пользователь уже имеет доступ как администратор Telegram-группы.');return
    # The confirmation callback is bound to this user's FSM, chat, target and short-lived nonce.
    token=secrets.token_hex(5)
    await state.set_state(Input.confirm)
    await state.set_data(dict(tool_chat_id=cid,operation='grant',target_id=uid,target_name=target.user.full_name,token=token,started_at=int(time.time())))
    await render(message,f'<b>{title(cid)}</b>\n\nНазначить {html.escape(target.user.full_name)} администратором Лоренцо только этого чата? Он сможет менять настройки, смотреть участников и получать пароль к веб-панели. Права Telegram-группы не изменятся.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b(cid,'✅ Назначить','apply:'+token),b(cid,'Отмена','home')]]))
    await clear_prompt(message,data,include_input=True)
