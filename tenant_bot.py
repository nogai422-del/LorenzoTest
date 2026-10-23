from __future__ import annotations
import html
import time
from aiogram import BaseMiddleware, Router, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, CallbackQuery
import tenancy as t
from db import get_conn, list_known_chats, record_chat
from runtime_paths import web_public_url

router = Router(name='tenant_commands')


@router.my_chat_member()
async def bot_membership(event):
    if event.chat.type not in {'group','supergroup'}:
        return
    cid=int(event.chat.id)
    active=event.new_chat_member.status not in {'left','kicked'}
    record_chat(cid,event.chat.title,event.chat.username,event.chat.type,int(time.time()),tracked=active,discovered_by='bot')
    if active and event.old_chat_member.status in {'left','kicked'}:
        await event.bot.send_message(cid,'Лоренцо подключён. Бесплатно доступны вопрос о возрасте и согласие на анкету. '
            'Администраторы могут открыть /admin: настройки и доступ относятся только к этому чату. /tariffs — платные уровни.')
    if not active:
        with get_conn() as c:
            c.execute('DELETE FROM tenant_admins WHERE chat_id=?',(cid,))
            c.execute('UPDATE tenant_tickets SET used=1 WHERE chat_id=?',(cid,))


class ChatBoundaryMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, 'chat', None) or getattr(getattr(event, 'message', None), 'chat', None)
        token = t.chat_context.set(int(chat.id) if chat and chat.type in {'group','supergroup'} else None)
        try:
            # Old callback buttons must never expose global lists or change global settings.
            if isinstance(event, CallbackQuery) and (event.data or '').startswith('adm:'):
                await event.answer('Старое меню закрыто. Откройте /admin в нужном чате.', show_alert=True)
                return
            return await handler(event, data)
        finally:
            t.chat_context.reset(token)


def menu(chat_id: int):
    level = t.tier(chat_id)
    rows = []
    if level < 1:
        rows.append([InlineKeyboardButton(text=f'Редактирование · {t.star_price(1)} ⭐', callback_data=f'tenant:buy:{chat_id}:1')])
    if level < 2:
        price = t.star_price(2) - (t.star_price(1) if level == 1 else 0)
        rows.append([InlineKeyboardButton(text=f'Полный доступ · {max(1,price)} ⭐', callback_data=f'tenant:buy:{chat_id}:2')])
    if level >= 2:
        rows.append([InlineKeyboardButton(text='Веб-панель · получить / обновить пароль', callback_data=f'tenant:panel:{chat_id}:2')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command('start','admin','activity','members','donate','tariffs'))
async def home(message: Message):
    if message.chat.type in {'group','supergroup'}:
        chat_id = int(message.chat.id)
        record_chat(chat_id, message.chat.title, message.chat.username, message.chat.type, int(time.time()), tracked=True, discovered_by='bot')
        if not await t.is_chat_admin(message.bot, chat_id, message.from_user.id):
            await message.answer('Тарифы и управление доступны администраторам этого чата.')
            return
        await t.refresh_admins(message.bot, chat_id)
        await show_menu(message, chat_id)
        return
    rows = []
    # Never show another chat's title before a live membership check.
    for chat in list_known_chats(include_untracked=True):
        cid = int(chat['chat_id'])
        if await t.is_chat_admin(message.bot, cid, message.from_user.id):
            rows.append([InlineKeyboardButton(text=chat['title'] or str(cid), callback_data=f'tenant:open:{cid}:0')])
    await message.answer('Выберите свой чат. Для подключения нового чата вызовите /admin внутри него.', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_menu(message, chat_id):
    level = t.tier(chat_id)
    await message.answer(
        f'<b>Лоренцо · управление чатом</b>\nID: <code>{chat_id}</code>\n'
        f'Уровень: {level}\n\nБесплатно: возраст и согласие на анкету.\n'
        f'1 · Редактирование: {t.star_price(1)} ⭐ разово (ориентир 50 ₽).\n'
        f'2 · Полный доступ: {t.star_price(2)} ⭐ разово (ориентир 100 ₽), включает уровень 1, активность, веб-панель и свой Telethon.\n'
        'Цена Stars в рублях зависит от способа покупки. Оплата действует на один чат.\n'
        'Настройки: <code>/edit reply_delay 2</code>, <code>/edit welcome_text Привет, {name}!</code>\n'
        'В личном чате: <code>/edit ID_ЧАТА параметр значение</code>.\n'
        '/terms — условия, /paysupport — поддержка.', reply_markup=menu(chat_id))


@router.callback_query(F.data.startswith('tenant:'))
async def action(call: CallbackQuery):
    try:
        _, action, cid, level = call.data.split(':')
        chat_id, level = int(cid), int(level)
    except (ValueError, AttributeError):
        await call.answer('Некорректная кнопка', show_alert=True)
        return
    if call.message.chat.type != 'private' and int(call.message.chat.id) != chat_id:
        await call.answer('Неверный чат', show_alert=True)
        return
    if not await t.is_chat_admin(call.bot, chat_id, call.from_user.id):
        await call.answer('Нет доступа к чату', show_alert=True)
        return
    if action == 'open':
        await show_menu(call.message, chat_id)
    elif action == 'panel':
        if t.tier(chat_id) < 2:
            await call.answer('Требуется полный доступ', show_alert=True)
            return
        if not web_public_url():
            await call.answer('Веб-адрес ещё не настроен', show_alert=True)
            return
        try:
            # Check that the private conversation is available before rotating credentials.
            await call.bot.send_chat_action(call.from_user.id,'typing')
            login,password = t.issue_credentials(chat_id, call.from_user.id)
            info=await call.bot.get_chat(chat_id)
            await call.bot.send_message(call.from_user.id,
                f'<b>Доступ к панели чата «{html.escape(info.title or str(chat_id))}»</b>\n'
                f'Логин: <code>{html.escape(login)}</code>\nПароль: <tg-spoiler>{html.escape(password)}</tg-spoiler>\n\n'
                'Это ваши личные данные входа только для этого чата. Сохраните пароль: бот не хранит его в открытом виде. '
                'Повторное нажатие «получить / обновить пароль» заменит пароль и завершит прежние сеансы. '
                'После снятия прав администратора вход будет закрыт.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть панель', url=web_public_url()+'/tenant/login')]]))
        except Exception:
            await call.answer('Сначала откройте личный чат с ботом и нажмите Start.', show_alert=True)
            return
    elif action == 'buy':
        if level not in (1,2) or t.tier(chat_id) >= level:
            await call.answer('Этот уровень уже доступен', show_alert=True)
            return
        if call.message.chat.type != 'private':
            await call.answer('Для оплаты откройте /admin в личном чате с ботом.', show_alert=True)
            return
        token, amount = t.create_order(chat_id, call.from_user.id, level)
        await call.bot.send_invoice(call.from_user.id, title='Лоренцо · '+('Редактирование' if level == 1 else 'Полный доступ'),
            description=f'Разовый доступ для чата {chat_id}. Нажимая оплату, вы принимаете /terms. Стоимость: {amount} Stars.',
            payload=token, currency='XTR', prices=[LabeledPrice(label='Доступ для одного чата', amount=amount)], provider_token='')
    await call.answer()


@router.pre_checkout_query()
async def checkout(query):
    order = t.get_order(query.invoice_payload)
    valid = bool(order and not order['paid'] and order['user_id'] == query.from_user.id and order['amount'] == query.total_amount
                 and query.currency == 'XTR' and t.tier(order['chat_id']) < order['tier'])
    if valid:
        valid = await t.is_chat_admin(query.bot, order['chat_id'], query.from_user.id)
    await query.answer(ok=valid, error_message=None if valid else 'Заказ устарел или нет прав администратора. Откройте /admin заново.')


@router.message(F.successful_payment)
async def payment(message):
    p = message.successful_payment
    t.confirm_payment(p.invoice_payload, message.from_user.id, p.total_amount, p.currency, p.telegram_payment_charge_id)
    order = t.get_order(p.invoice_payload)
    await message.answer(f'Оплата подтверждена. Уровень {order["tier"]} доступен чату {order["chat_id"]}.')


@router.message(F.refunded_payment)
async def refunded(message):
    t.record_refund(message.refunded_payment.telegram_payment_charge_id)


@router.message(Command('edit'))
async def edit(message):
    parts = (message.text or '').split(maxsplit=3)
    try:
        if message.chat.type == 'private':
            cid, key, value = int(parts[1]), parts[2], parts[3]
        else:
            cid = int(message.chat.id)
            _, key, value = message.text.split(maxsplit=2)
        if not await t.is_chat_admin(message.bot, cid, message.from_user.id) or t.tier(cid) < 1:
            raise ValueError('Нужны права администратора и платный уровень')
        cfg = t.settings(cid)
        if key in {'welcome_text','consent_text','nudge_text'}:
            allowed = {'welcome_text': {'name'}, 'consent_text': {'age'}, 'nudge_text': {'mention','name','username','user_id'}}[key]
            import string
            if len(value) > 1500 or any(field and field not in allowed for _,field,_,_ in string.Formatter().parse(value)):
                raise ValueError('Недопустимый шаблон')
            cfg['texts'][key] = value
        elif key == 'reply_delay':
            cfg[key] = max(0,min(60,int(value)))
        elif key == 'is_active':
            if value not in {'0','1'}: raise ValueError('Укажите 0 или 1')
            cfg[key] = value == '1'
        elif key in {'work_start','work_end'}:
            from datetime import datetime
            datetime.strptime(value,'%H:%M')
            cfg[key] = value
        else:
            raise ValueError('Доступны welcome_text, consent_text, nudge_text, reply_delay, is_active, work_start, work_end')
        t.save_chat_settings(cid,cfg)
        await message.answer('Настройки этого чата сохранены.')
    except (ValueError,IndexError) as exc:
        await message.answer(html.escape(str(exc)) or 'Проверьте команду /edit')


@router.message(Command('creator'))
async def creator_link(message):
    import os
    if message.chat.type=='private' and message.from_user.id==int(os.getenv('OWNER_ID','0')) and web_public_url():
        await message.answer('Ваша панель создателя: доход, охват, чаты, объявления и вход в чаты.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Панель создателя',url=web_public_url()+'/creator')]]))


@router.message(Command('terms','paysupport','support'))
async def support(message):
    await message.answer('Доступ приобретается разово для одного чата. Полный уровень включает редактирование; при повышении оплачивается разница. '
                         'Доступ к управлению есть только у текущих Telegram-администраторов чата. '
                         'Вопросы по оплате и возврату — владельцу бота: '+f'<a href="tg://user?id={int(__import__("os").getenv("OWNER_ID","0"))}">написать</a>.')
