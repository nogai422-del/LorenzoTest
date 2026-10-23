"""Creator-only moderation journal of group updates received by the bot."""
from __future__ import annotations
import os
import time
from aiogram import BaseMiddleware
from aiogram.types import Message
from db import get_conn,get_chat_info,record_chat

_last_cleanup=0


def retention_days():
    try:return max(1,min(365,int(os.getenv('DIALOGUE_LOG_RETENTION_DAYS','30'))))
    except ValueError:return 30


def init_dialogue_log():
    with get_conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS dialogue_log (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, revision INTEGER NOT NULL,
            author_id INTEGER, author_name TEXT NOT NULL, username TEXT,
            author_type TEXT NOT NULL, text TEXT NOT NULL, content_type TEXT NOT NULL,
            reply_to INTEGER, sent_at INTEGER NOT NULL, version_at INTEGER NOT NULL,
            is_edit INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id,message_id,revision));
        CREATE INDEX IF NOT EXISTS idx_dialogue_time ON dialogue_log(version_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dialogue_chat_time ON dialogue_log(chat_id,version_at DESC);
        ''')
    cleanup()


def cleanup():
    global _last_cleanup
    cutoff=int(time.time())-retention_days()*86400
    with get_conn() as c:
        c.execute('DELETE FROM dialogue_log WHERE version_at<?',(cutoff,))
    _last_cleanup=time.monotonic()


def record_message(message: Message):
    # Private conversations, passwords, login codes and payments sent to the bot are never ingested.
    if message.chat.type not in {'group','supergroup'}:
        return
    if message.new_chat_members or message.left_chat_member:
        return
    if message.from_user and message.from_user.is_bot and not message.sender_chat:
        return
    if not message.text and not message.caption and message.content_type=='text':
        return
    info=get_chat_info(message.chat.id) or {}
    if info.get('is_hidden'):
        return
    if time.monotonic()-_last_cleanup>3600:
        cleanup()
    sender=message.sender_chat or message.from_user
    if not sender:return
    sender_chat=message.sender_chat is not None
    name=(getattr(sender,'title',None) if sender_chat else sender.full_name) or str(sender.id)
    sent=int(message.date.timestamp())
    edited=int(message.edit_date.timestamp() if hasattr(message.edit_date,"timestamp") else message.edit_date) if message.edit_date else 0
    version_at=edited or sent
    if version_at<int(time.time())-retention_days()*86400:
        return
    record_chat(message.chat.id,message.chat.title,message.chat.username,message.chat.type,tracked=True,discovered_by='bot')
    with get_conn() as c:
        c.execute('''INSERT INTO dialogue_log(chat_id,message_id,revision,author_id,author_name,username,
                  author_type,text,content_type,reply_to,sent_at,version_at,is_edit)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(chat_id,message_id,revision) DO UPDATE SET text=excluded.text,
                  author_name=excluded.author_name,username=excluded.username''',
                  (message.chat.id,message.message_id,edited,sender.id,name,getattr(sender,'username',None),
                   'chat' if sender_chat else 'user',(message.text or message.caption or '')[:16000],str(getattr(message.content_type,'value',message.content_type)),
                   message.reply_to_message.message_id if message.reply_to_message else None,sent,version_at,int(bool(edited))))


class DialogueMiddleware(BaseMiddleware):
    async def __call__(self,handler,event,data):
        if isinstance(event,Message):
            try:record_message(event)
            except Exception:
                # No message content or credentials may end up in technical error logs.
                import logging
                logging.getLogger('dialogue_log').error('Could not persist group message: chat=%s message=%s',event.chat.id,event.message_id)
        return await handler(event,data)


def query_messages(chat_id=None,author='',q='',before=None,limit=100):
    clauses=['d.version_at>=?']
    args=[int(time.time())-retention_days()*86400]
    if chat_id is not None:
        clauses.append('d.chat_id=?');args.append(int(chat_id))
    if author.strip():
        clauses.append('(CAST(d.author_id AS TEXT)=? OR d.username=? OR d.author_name LIKE ? ESCAPE \'\\\')')
        raw=author.strip().lstrip('@')[:100]
        escaped=raw.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        args.extend([raw,raw,'%'+escaped+'%'])
    if q.strip():
        clauses.append('d.text LIKE ? ESCAPE \'\\\'')
        escaped=q.strip()[:200].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        args.append('%'+escaped+'%')
    if before is not None:
        clauses.append('d.rowid<?');args.append(int(before))
    args.append(max(1,min(200,int(limit))))
    with get_conn() as c:
        return c.execute('''SELECT d.rowid AS cursor,d.*,c.title AS chat_title,c.username AS chat_username
                         FROM dialogue_log d JOIN chats c ON c.chat_id=d.chat_id
                         WHERE COALESCE(c.is_hidden,0)=0 AND '''+' AND '.join(clauses)+
                         ' ORDER BY d.rowid DESC LIMIT ?',args).fetchall()


async def cleanup_loop():
    import asyncio,logging
    while True:
        try:cleanup()
        except Exception:logging.getLogger('dialogue_log').error('Dialogue retention cleanup failed')
        await asyncio.sleep(3600)
