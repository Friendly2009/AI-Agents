import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app_factory import make_app
from brain import SCHEMA
from delivery import GuardedTelegramClient
from telethon import TelegramClient


class DirectChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_routes(self):
        app = make_app()
        self.addCleanup(app.close)
        handlers = {}

        def register(_):
            def save(fn):
                handlers[fn.__name__] = fn
                return fn
            return save

        @asynccontextmanager
        async def typing(*args):
            yield

        client = SimpleNamespace(on=register, action=typing, get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
                                 generated_saved_message=AsyncMock(return_value=False),
                                 reply_to_saved_command=AsyncMock(), reply_to_admin_chat=AsyncMock(),
                                 send_message=AsyncMock(), run_until_disconnected=AsyncMock())

        def event(uid, mid, text, out=False):
            return SimpleNamespace(is_private=True, out=out, sender_id=uid, chat_id=uid, id=mid,
                                   raw_text=text, message=SimpleNamespace(voice=False),
                                   get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)), reply=AsyncMock())

        answer = dict.fromkeys(SCHEMA['required'])
        answer.update(reply='Обсудим подробнее.', wants_trial=False, handoff=False, next_action='answer')
        app.respond = AsyncMock(return_value=answer)
        with patch.dict(os.environ, {'SAFE_TEST_MODE':'true', 'ADMIN_OWNER_TG_ID':'20', 'ADMIN_WIFE_TG_ID':''}), \
             patch('telegram_adapter.init_catalog'), patch('telegram_adapter.recover_opening',new_callable=AsyncMock):
            await app.listen(client)
            await handlers['owner_command'](event(10, 1, 'Как дела?', True))
            client.reply_to_saved_command.assert_awaited_once_with(1, answer['reply'])
            client.generated_saved_message.return_value = True
            await handlers['owner_command'](event(10, 2, answer['reply'], True))
            self.assertEqual(app.respond.await_count, 1)
            await handlers['on_message'](event(20, 3, 'А чем занимаешься?'))
            client.reply_to_admin_chat.assert_awaited_once_with(20, 3, answer['reply'])
            self.assertEqual(len(app.respond.await_args.args[0]), 1)
            await handlers['on_message'](event(20, 4, 'Расскажи подробнее'))
            self.assertEqual(len(app.respond.await_args.args[0]), 3)
            self.assertFalse(app.STORE.get(20))
            with patch.dict(os.environ, {'SAFE_TEST_MODE':'false'}):
                await handlers['on_message'](event(30, 5, 'Сколько стоит?'))
                client.send_message.assert_awaited_once_with(30, answer['reply'])
                self.assertFalse(app.STORE.get(30)['crm_consent'])
                identity = event(30, 6, 'Ты бот?')
                await handlers['on_message'](identity)
                self.assertIn('ИИ', identity.reply.await_args.args[0])
                capability = event(30, 7, 'Что ты умеешь?')
                await handlers['on_message'](capability)
                self.assertIn('расписание', capability.reply.await_args.args[0])

    async def test_saved_reply_ledger(self):
        app = make_app()
        self.addCleanup(app.close)
        client = GuardedTelegramClient(None, 1, 'abc')
        client.configure_delivery(app.STORE, {10}, app.M)
        client.saved_messages_user_id = 10
        with patch.object(TelegramClient, 'send_message', AsyncMock(return_value=SimpleNamespace(id=25))) as send:
            await client.reply_to_saved_command(24, 'Здравствуйте!')
            self.assertTrue(await client.generated_saved_message(25))
            self.assertFalse(await client.generated_saved_message(24))
            await client.reply_to_saved_command(24, 'Здравствуйте!')
            send.assert_awaited_once()
