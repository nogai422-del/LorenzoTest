"""Check actual Bot API access; historical/Telethon discovery is not membership."""
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError


def telegram_error(exc):
    if isinstance(exc,TelegramRetryAfter):
        return f'Telegram ограничил отправку. Повторите через {exc.retry_after} сек.'
    if isinstance(exc,TelegramForbiddenError):
        return 'Telegram запретил боту доступ. Добавьте Лоренцо в чат и разрешите ему отправлять сообщения.'
    if isinstance(exc,TelegramBadRequest):
        msg=exc.message.lower()
        if 'chat not found' in msg:
            return 'Чат недоступен самому Лоренцо: бот не добавлен, удалён или сохранён устаревший ID. Добавьте @Lorenzo_nk_bot в нужный чат и вызовите там /admin. Присутствия вашего Telethon-аккаунта недостаточно.'
        if 'not enough rights' in msg or 'have no rights' in msg or 'write_forbidden' in msg:
            return 'Боту запрещено писать в чат. Проверьте разрешение на отправку сообщений в настройках группы.'
        return 'Telegram отклонил сообщение. Проверьте доступ бота и ограничения выбранного чата.'
    return 'Не удалось связаться с Telegram. Сообщение не подтверждено как отправленное; проверьте чат перед повторной попыткой.'


async def destination_status(bot,chat_id):
    try:
        member=await bot.get_chat_member(chat_id,bot.id)
        if member.status in {'left','kicked'}:
            return False,'Лоренцо не состоит в чате. Добавьте бота и вызовите /admin в группе.'
        if member.status=='restricted' and not getattr(member,'can_send_messages',False):
            return False,'У Лоренцо нет разрешения отправлять сообщения.'
        if member.status!='administrator':
            return False,'Лоренцо приостановлен: назначьте бота администратором этого чата. Работа без прав администратора отключена.'
        return True,'Доступ к чату подтверждён'
    except (TelegramBadRequest,TelegramForbiddenError,TelegramRetryAfter,TelegramNetworkError) as exc:
        return False,telegram_error(exc)
