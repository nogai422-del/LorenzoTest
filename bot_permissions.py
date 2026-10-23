"""A chat is operational only while Lorenzo has Telegram administrator status."""
import time

WARNING = ('⚠️ Лоренцо приостановлен: мне нужны права администратора этого чата.\n\n'
           'Назначьте @Lorenzo_nk_bot администратором группы. Для модерации также разрешите удалять сообщения '
           'и ограничивать участников. После назначения работа возобновится автоматически.\n\n'
           'До этого онбординг, сбор активности, синхронизация и управление чатом не работают.')
_status = {}
_notices = {}


def update_status(chat_id, status):
    _status[int(chat_id)] = (time.monotonic(), status == 'administrator')


async def has_admin_rights(bot,chat_id,*,fresh=False):
    cid=int(chat_id)
    known=_status.get(cid)
    if known and not fresh and time.monotonic()-known[0]<15:
        return known[1]
    try:
        member=await bot.get_chat_member(cid,bot.id)
        update_status(cid,member.status)
        return member.status=='administrator'
    except Exception:
        _status.pop(cid,None)
        return False


async def warn(bot,chat_id,*,force=False,inviter_id=None):
    cid=int(chat_id)
    now=time.monotonic()
    if not force and now-_notices.get(cid,-100000)<3600:
        return
    _notices[cid]=now
    try:
        await bot.send_message(cid,WARNING,parse_mode=None)
    except Exception:
        if inviter_id:
            try:
                await bot.send_message(inviter_id,WARNING+'\nID чата: '+str(cid),parse_mode=None)
            except Exception:
                pass
