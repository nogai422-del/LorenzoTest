"""Telegram settings UI; every action and pending edit belongs to one chat."""
from __future__ import annotations
import html
import re
import string
import time
from aiogram import Router,F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command,StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup,ForceReply
from db import get_chat_info,get_chat_settings,set_chat_settings,audit
import tenancy as t

router=Router(name='chat_settings_telegram')


class Editing(StatesGroup):
    value=State()


FIELDS={
    'welcome_text':('Текст приветствия','Текст до 1500 символов. Для обращения используйте {name}.'),
    'consent_text':('Текст анкеты','Текст до 1500 символов. Возраст подставляется через {age}.'),
    'nudge_text':('Текст напоминания','Текст до 1500 символов. Доступны {mention}, {name}, {username}, {user_id}.'),
    'hours':('Часы работы','Введите время в формате 07:00–19:00. Часовой пояс: Москва. Для круглосуточной работы: 00:00–23:59.'),
    'reply_delay':('Задержка ответа','Введите целое число секунд от 0 до 360.'),
    'inactivity_days':('Дней без сообщений','Введите число дней от 1 до 3650.'),
    'min_message_count':('Минимум сообщений','Введите число от 0 до 1000000 за период неактивности. 0 отключает этот порог.'),
    'repeat_alert_hours':('Повтор оповещения','Введите интервал в часах от 1 до 8760.'),
    'check_interval_minutes':('Частота проверки','Введите интервал в минутах от 1 до 10080.'),
}
ACTIVITY_FIELDS={'inactivity_days','min_message_count','repeat_alert_hours','check_interval_minutes'}


def button(cid,label,action):
    return InlineKeyboardButton(text=label,callback_data=f'cfg:{cid}:{action}')


def title(cid):
    info=get_chat_info(cid) or {}
    return html.escape(info.get('title') or str(cid))


async def permitted(bot,cid,uid,conversation_id,conversation_type,minimum=1):
    if conversation_type!='private' and int(conversation_id)!=int(cid):return False
    info=get_chat_info(cid) or {}
    if info.get('is_hidden') or info.get('chat_type') not in {'group','supergroup'}:return False
    return t.tier(cid)>=minimum and await t.is_chat_admin(bot,cid,uid)


def parse_value(key,value):
    value=value.strip()
    if key in {'welcome_text','consent_text','nudge_text'}:
        allowed={'welcome_text':{'name'},'consent_text':{'age'},'nudge_text':{'mention','name','username','user_id'}}[key]
        if not value or len(value)>1500:raise ValueError('Нужно от 1 до 1500 символов.')
        try:
            for _,field,spec,conversion in string.Formatter().parse(value):
                if field is not None and (field not in allowed or spec or conversion):raise ValueError()
        except ValueError:raise ValueError('Проверьте фигурные скобки. Доступные подстановки: '+', '.join('{'+f+'}' for f in sorted(allowed)))
        return value
    if key=='hours':
        match=re.fullmatch(r'(\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})',value)
        if not match:raise ValueError('Пример расписания: 07:00–19:00.')
        for clock in match.groups():
            h,m=map(int,clock.split(':'))
            if h>23 or m>59:raise ValueError('Время должно быть в диапазоне 00:00–23:59.')
        return match.groups()
    bounds={'reply_delay':(0,360),'inactivity_days':(1,3650),'min_message_count':(0,1000000),'repeat_alert_hours':(1,8760),'check_interval_minutes':(1,10080)}
    if key not in bounds:raise ValueError('Неизвестная настройка.')
    try:number=int(value)
    except ValueError:raise ValueError('Введите целое число.')
    lo,hi=bounds[key]
    if not lo<=number<=hi:raise ValueError(f'Допустимо от {lo} до {hi}.')
    return number


def save_value(cid,key,value):
    if key in ACTIVITY_FIELDS:
        set_chat_settings(cid,**{key:value})
    else:
        changes={'work_start':value[0],'work_end':value[1]} if key=='hours' else {key:value}
        t.patch_chat_settings(cid,changes)


async def show_settings(message,cid,section='home',replace=False):
    cfg=t.settings(cid)
    heading=f'⚙️ <b>{title(cid)}</b>\n'
    if section=='activity':
        alerts=get_chat_settings(cid)
        text=(heading+'<b>Неактивность и оповещения</b>\n\n'
              f'Оповещения: {"включены" if alerts["enabled"] else "выключены"}\n'
              f'Период без сообщений: {alerts["inactivity_days"]} дн.\n'
              f'Минимум сообщений за период: {alerts["min_message_count"]}\n'
              f'Повтор оповещения: {alerts["repeat_alert_hours"]} ч.\n'
              f'Проверка каждые: {alerts["check_interval_minutes"]} мин.\n\n'
              'Получатели — текущие администраторы именно этого чата.')
        rows=[[button(cid,'🔕 Выключить оповещения' if alerts['enabled'] else '🔔 Включить оповещения','set_alerts:'+('0' if alerts['enabled'] else '1'))]]
        rows += [[button(cid,FIELDS[k][0],'edit:'+k)] for k in ('inactivity_days','min_message_count','repeat_alert_hours','check_interval_minutes')]
        rows.append([button(cid,'‹ Настройки чата','home')])
    elif section=='levels':
        text=(heading+f'<b>Уровни сценария</b> · сейчас {cfg["level"]}\n\n'
              '1 — спросить возраст.\n2 — спросить возраст и согласие на анкету.\n'
              '3 — возраст, анкета и строгая проверка ссылок/@упоминаний во время онбординга. '
              'В строгом режиме подозрительное сообщение удаляется, участник блокируется при наличии у бота соответствующих прав.\n\n'
              'Это сценарии поведения, а не платные тарифы. Уровень меняется только для выбранного чата.')
        rows=[[button(cid,('✅ ' if cfg['level']==n else '')+f'Уровень {n}',f'level:{n}')] for n in (1,2,3)]
        rows.append([button(cid,'‹ Настройки чата','home')])
    elif section=='texts':
        text=heading+'<b>Тексты бота</b>\n\n'
        for key in ('welcome_text','consent_text','nudge_text'):
            text+='<b>'+FIELDS[key][0]+'</b>\n'+html.escape(cfg['texts'][key][:600])+'\n\n'
        rows=[[button(cid,FIELDS[k][0],'edit:'+k)] for k in ('welcome_text','consent_text','nudge_text')]
        rows.append([button(cid,'‹ Настройки чата','home')])
    else:
        text=(heading+'<b>Настройки бота в Telegram</b>\n\n'
              f'Онбординг: {"включён" if cfg["is_active"] else "выключен"}\n'
              f'Уровень сценария: {cfg["level"]}\n'
              f'Часы работы: {html.escape(cfg["work_start"])}–{html.escape(cfg["work_end"])} · МСК\n'
              f'Задержка ответа: {cfg["reply_delay"]} сек.\n\n'
              'Изменения сохраняются только для этого чата. Выберите раздел:')
        rows=[[button(cid,'🔴 Выключить онбординг' if cfg['is_active'] else '🟢 Включить онбординг','set_active:'+('0' if cfg['is_active'] else '1'))],
              [button(cid,'🎚 Уровни сценария','levels')],
              [button(cid,'✏️ Тексты бота','texts')],
              [button(cid,'🕒 Часы работы','edit:hours'),button(cid,'⏱ Задержка','edit:reply_delay')]]
        if t.tier(cid)>=2:
            rows.append([button(cid,'🔔 Неактивность и оповещения','activity')])
            from aiogram.types import InlineKeyboardButton
            rows.append([InlineKeyboardButton(text='👥 Участники, админы и статистика',callback_data=f'tool:{cid}:home')])
        rows.append([InlineKeyboardButton(text='‹ Меню этого чата',callback_data=f'tenant:open:{cid}:0')])
    send=message.edit_text if replace else message.answer
    await send(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('cfg:'))
async def settings_callback(call,state:FSMContext):
    try:
        _,raw,action=call.data.split(':',2);cid=int(raw)
    except (ValueError,AttributeError):
        await call.answer('Некорректная кнопка.',show_alert=True);return
    activity=(action=='activity' or action.startswith('set_alerts:')) or action.removeprefix('edit:') in ACTIVITY_FIELDS
    if not call.message or not await permitted(call.bot,cid,call.from_user.id,call.message.chat.id,call.message.chat.type,2 if activity else 1):
        await call.answer('Нет доступа: нужны права администратора этого чата и соответствующий тариф.',show_alert=True);return
    await state.clear()
    if action=='cancel':
        await show_settings(call.message,cid,replace=True)
    elif action in {'home','texts','activity','levels'}:
        await show_settings(call.message,cid,action,replace=True)
    elif action in {'level:1','level:2','level:3'}:
        t.patch_chat_settings(cid,{'level':int(action[-1])})
        audit('tg:'+str(call.from_user.id),'chat_scenario_level_changed',str(cid),{'level':int(action[-1])})
        await show_settings(call.message,cid,'levels',replace=True)
    elif action in {'set_active:0','set_active:1'}:
        t.patch_chat_settings(cid,{'is_active':action.endswith(':1')})
        audit('tg:'+str(call.from_user.id),'chat_onboarding_toggled',str(cid))
        await show_settings(call.message,cid,replace=True)
    elif action in {'set_alerts:0','set_alerts:1'}:
        set_chat_settings(cid,enabled=action.endswith(':1'))
        audit('tg:'+str(call.from_user.id),'chat_alerts_toggled',str(cid))
        await show_settings(call.message,cid,'activity',replace=True)
    elif action.startswith('edit:') and action[5:] in FIELDS:
        key=action[5:]
        prompt=await call.message.answer(f'✏️ <b>{title(cid)} · {FIELDS[key][0]}</b>\n\n{html.escape(FIELDS[key][1])}\n\nОтправьте новое значение ответом на это сообщение. Для отмены: /cancel.',reply_markup=ForceReply(selective=True))
        await state.set_state(Editing.value)
        await state.update_data(settings_chat_id=cid,settings_field=key,prompt_id=prompt.message_id,started_at=int(time.time()))
    else:
        await call.answer('Эта кнопка устарела. Откройте настройки заново.',show_alert=True);return
    await call.answer()


@router.message(StateFilter(Editing.value),Command('cancel'))
async def cancel_edit(message,state:FSMContext):
    await state.clear()
    await message.answer('Изменение отменено. /admin — выбрать чат и открыть настройки.')


@router.message(StateFilter(Editing.value),F.text & ~F.text.startswith('/'))
async def settings_value(message,state:FSMContext):
    data=await state.get_data()
    cid=data.get('settings_chat_id');key=data.get('settings_field')
    if not cid or key not in FIELDS or int(time.time())-data.get('started_at',0)>900:
        await state.clear();await message.answer('Время ввода истекло. Откройте настройки через /admin.');return
    if message.chat.type!='private' and (not message.reply_to_message or message.reply_to_message.message_id!=data.get('prompt_id')):
        return UNHANDLED
    if not await permitted(message.bot,cid,message.from_user.id,message.chat.id,message.chat.type,2 if key in ACTIVITY_FIELDS else 1):
        await state.clear();await message.answer('Изменения не сохранены: доступ к выбранному чату отозван.');return
    try:value=parse_value(key,message.text)
    except ValueError as exc:
        await message.answer(html.escape(str(exc))+'\nОтправьте исправленное значение или /cancel.');return
    save_value(cid,key,value)
    audit('tg:'+str(message.from_user.id),'chat_setting_changed',str(cid),{'field':key})
    await state.clear()
    await message.answer(f'✅ <b>{title(cid)}</b>: настройка «{FIELDS[key][0]}» сохранена.')
    await show_settings(message,cid,'activity' if key in ACTIVITY_FIELDS else 'texts' if key.endswith('_text') else 'home')
