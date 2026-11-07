"""One persistent interactive menu per conversation and user; only UI transients are deleted."""
from contextvars import ContextVar
from aiogram.exceptions import TelegramBadRequest
from db import get_conn

actor_context=ContextVar('telegram_ui_actor',default=None)


async def delete_transient(bot,conversation_id,message_id):
    if not isinstance(message_id,int):return
    try:await bot.delete_message(conversation_id,message_id)
    except Exception:pass


async def clear_prompt(message,data,include_input=False):
    if data.get('prompt_id'):await delete_transient(message.bot,message.chat.id,data['prompt_id'])
    if include_input:await delete_transient(message.bot,message.chat.id,getattr(message,'message_id',None))


async def render(message,text,reply_markup,replace=False,fresh=False):
    cid=int(message.chat.id)
    uid=actor_context.get() or getattr(getattr(message,'from_user',None),'id',None)
    if not uid and message.chat.type=='private':uid=cid
    with get_conn() as c:
        row=c.execute('SELECT message_id FROM tenant_ui_windows WHERE conversation_id=? AND user_id=?',(cid,uid)).fetchone() if uid else None
    current_id=getattr(message,'message_id',None)
    previous=row['message_id'] if row else None
    target=None if fresh else previous or (current_id if replace and isinstance(current_id,int) else None)
    result=None
    if not fresh and (target is not None or replace):
        try:
            if replace and (not previous or previous==current_id):result=await message.edit_text(text,reply_markup=reply_markup)
            else:result=await message.bot.edit_message_text(text,chat_id=cid,message_id=target,reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' in exc.message.lower():result=target
            elif 'not found' in exc.message.lower() or "can't be edited" in exc.message.lower():target=None
            else:raise
    if result is None and target is None:result=await message.answer(text,reply_markup=reply_markup)
    result_id=result if type(result) is int else getattr(result,'message_id',None)
    if not isinstance(result_id,int):result_id=target
    if uid and isinstance(result_id,int):
        with get_conn() as c:c.execute('INSERT INTO tenant_ui_windows VALUES (?,?,?) ON CONFLICT(conversation_id,user_id) DO UPDATE SET message_id=excluded.message_id',(cid,uid,result_id))
        # Remove only an obsolete bot controller that was actually tracked as a menu.
        if previous and previous!=result_id:await delete_transient(message.bot,cid,previous)
    return result


async def input_error(message,text,cancel_callback):
    from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
    await render(message,text+'\n\nОтветьте на запрос ещё раз или нажмите «Отмена».',InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена',callback_data=cancel_callback)]]))
    await delete_transient(message.bot,message.chat.id,getattr(message,'message_id',None))
