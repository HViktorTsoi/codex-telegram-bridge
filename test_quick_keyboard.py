import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
import unittest

from bridge import TelegramError
from quick_keyboard import BUTTON_COMMANDS, QuickKeyboard, markup, normalize_update


class KeyboardProjectionTests(unittest.TestCase):
    def message(self, text='📚 会话', **extra):
        return {'update_id': 9, 'message': {'message_id': 30,
            'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'message_thread_id': 42, 'text': text,
            'reply_to_message': {'message_id': 20, 'message_thread_id': 42, 'text': '原文'}, **extra}}

    def test_all_exact_shortcuts_retain_topic_reply_and_original_object(self):
        for label, command in BUTTON_COMMANDS.items():
            with self.subTest(label=label):
                update = self.message(label)
                before = copy.deepcopy(update)
                result = normalize_update(update)
                self.assertEqual(update, before)
                self.assertIsNot(result, update)
                self.assertEqual(result['message']['text'], command)
                self.assertEqual(result['message']['message_thread_id'], 42)
                self.assertEqual(result['message']['reply_to_message'], before['message']['reply_to_message'])
                self.assertEqual(set(result), set(update))
                self.assertEqual(set(result['message']), set(update['message']))

    def test_normal_messages_media_forwarded_text_and_protected_replies_are_unchanged(self):
        examples = [self.message('📚 会话 '), self.message('请打开 📚 会话'), self.message('/sessions'),
            self.message(None), self.message(['invalid']), self.message(photo=[], caption='📚 会话'),
            self.message(document={'file_id': 'id'}), self.message(caption='原说明'),
            self.message(forward_origin={'type': 'user'}), self.message(forward_origin=None),
            self.message(entities=[{'type': 'bold', 'offset': 0, 'length': 4}]),
            {'update_id': 9, 'callback_query': {'data': '📚 会话'}}, {'update_id': 9}]
        for update in examples:
            with self.subTest(update=update):
                self.assertIs(normalize_update(update), update)
        protected = self.message()
        self.assertIs(normalize_update(protected, protected=True), protected)

    def test_markup_is_single_row_persistent_theme_default_and_returns_fresh_objects(self):
        value = markup()
        self.assertEqual([len(row) for row in value['keyboard']], [3])
        buttons = [button for row in value['keyboard'] for button in row]
        self.assertEqual([button['text'] for button in buttons], ['📚 会话', '📋 任务', '⏹ 停止'])
        self.assertTrue(value['is_persistent'])
        self.assertTrue(value['resize_keyboard'])
        self.assertFalse(value['one_time_keyboard'])
        self.assertNotIn('inline_keyboard', value)
        self.assertTrue(all(set(button) == {'text'} for button in buttons))
        value['keyboard'][0][0]['text'] = 'mutated'
        self.assertEqual(markup()['keyboard'][0][0]['text'], '📚 会话')


class QuickKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT INTO kv VALUES('offset','100')")
        self.db.commit()
        self.now, self.allowed, self.calls = 1000, True, []
        self.responses = []
        test = self

        class Telegram:
            async def call(self, method, **params):
                if method != 'sendMessage':
                    raise AssertionError('Keyboard must not poll, query native settings or start a model: ' + method)
                test.calls.append((method, copy.deepcopy(params)))
                response = test.responses.pop(0) if test.responses else {
                    'message_id': 100 + len(test.calls), 'chat': {'id': 123}, 'from': {'id': 444}}
                if isinstance(response, Exception):
                    raise response
                return response

        store = SimpleNamespace(db=self.db, offset=lambda: int(self.db.execute(
            "SELECT value FROM kv WHERE key='offset'").fetchone()[0]))
        self.router = SimpleNamespace(config={'expected_bot_username': '@test_bot'},
            store=store, owner='123', bot_id=444, username='test_bot', telegram=Telegram(),
            owner_allowed=lambda: self.allowed,
            access=lambda: {'dmPolicy': 'allowlist', 'allowFrom': ['123']})
        self.keyboard = QuickKeyboard(self.router, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.db.close()

    def command(self, update_id=101, text='/keyboard', **extra):
        return {'update_id': update_id, 'message': {'message_id': update_id + 100,
            'text': text, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, **extra}}

    def jobs(self):
        return self.db.execute('SELECT * FROM quick_keyboard_jobs ORDER BY rowid').fetchall()

    async def test_startup_sends_once_in_all_and_restart_does_not_send_again(self):
        self.assertTrue(self.keyboard.ensure_startup())
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertTrue(await self.keyboard.handle_one())
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(len(self.calls), 1)
        method, params = self.calls[0]
        self.assertEqual((method, params['chat_id']), ('sendMessage', '123'))
        self.assertNotIn('message_thread_id', params)
        self.assertTrue(params['disable_notification'])
        self.assertEqual(params['reply_markup'], markup())
        self.assertEqual(self.keyboard.state()['state'], 'sent')
        restarted = QuickKeyboard(self.router, clock=lambda: self.now)
        restarted.recover()
        self.assertFalse(restarted.ensure_startup())
        self.assertFalse(await restarted.handle_one())
        self.assertEqual(len(self.calls), 1)

    async def test_manual_keyboard_request_is_durable_idempotent_and_chat_wide(self):
        update = self.command(message_thread_id=42,
            entities=[{'type': 'bot_command', 'offset': 0, 'length': 9}])
        self.assertTrue(self.keyboard.ingest(update))
        self.assertTrue(self.keyboard.ingest(copy.deepcopy(update)))
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.router.store.offset(), 102)
        self.assertTrue(await self.keyboard.handle_one())
        self.assertNotIn('message_thread_id', self.calls[0][1])
        self.assertEqual(self.jobs()[0]['origin_topic_id'], 42)
        self.assertTrue(self.keyboard.ingest(update))
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(len(self.calls), 1)

    async def test_manual_request_can_reopen_after_success_without_repeating_startup(self):
        self.keyboard.ensure_startup()
        await self.keyboard.handle_one()
        self.assertTrue(self.keyboard.ingest(self.command()))
        await self.keyboard.handle_one()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.keyboard.ensure_startup())

    async def test_manual_request_before_first_startup_is_the_only_installation_intent(self):
        self.assertTrue(self.keyboard.ingest(self.command()))
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertEqual(len(self.jobs()), 1)
        await self.keyboard.handle_one()
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(len(self.calls), 1)

    async def test_unauthorized_forwarded_media_and_parameter_commands_do_not_enqueue(self):
        updates = [self.command(text='/keyboard extra'), self.command(text='/keyboard@foreign'),
            self.command(text='keyboard'), self.command(photo=[], caption='/keyboard'),
            self.command(forward_origin={'type': 'user'}), self.command(text=None),
            self.command(**{'from': {'id': 999}}),
            self.command(chat={'id': 123, 'type': 'group'})]
        for update in updates:
            with self.subTest(update=update):
                self.assertFalse(self.keyboard.ingest(update))
        self.assertFalse(self.jobs())
        self.assertEqual(self.router.store.offset(), 100)

    async def test_startup_waits_for_verified_bot_and_owner_identity(self):
        self.router.bot_id = None
        self.assertFalse(self.keyboard.ensure_startup())
        self.router.bot_id, self.router.username = 444, 'another_bot'
        self.assertFalse(self.keyboard.ensure_startup())
        self.router.username = 'test_bot'
        self.allowed = False
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertFalse(self.keyboard.ingest(self.command()))
        self.assertFalse(self.jobs())
        self.allowed = True
        self.assertTrue(self.keyboard.ensure_startup())

    async def test_disable_option_preserves_existing_keyboard_and_queues_nothing(self):
        self.router.config['quick_keyboard'] = False
        disabled = QuickKeyboard(self.router)
        self.assertFalse(disabled.enabled)
        self.assertFalse(disabled.ensure_startup())
        self.assertFalse(disabled.ingest(self.command()))
        self.assertFalse(await disabled.handle_one())
        self.assertFalse(self.calls)

    async def test_permission_revocation_before_send_defers_without_contacting_telegram(self):
        self.keyboard.ensure_startup()
        self.allowed = False
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(self.jobs()[0]['state'], 'pending')
        self.assertFalse(self.calls)

    async def test_unknown_delivery_is_not_retried_but_new_explicit_request_is_allowed(self):
        self.keyboard.ensure_startup()
        self.responses.append(ConnectionError('ACK lost'))
        await self.keyboard.handle_one()
        self.assertEqual(self.jobs()[0]['state'], 'uncertain')
        self.keyboard.recover()
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(len(self.calls), 1)
        self.keyboard.ingest(self.command())
        await self.keyboard.handle_one()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.jobs()[-1]['state'], 'sent')

    async def test_bad_ack_is_uncertain_instead_of_creating_a_second_message(self):
        for response in ({}, {'message_id': True}, {'message_id': 99, 'chat': {'id': 999}},
                         {'message_id': 99, 'from': {'id': 555}}):
            with self.subTest(response=response):
                self.keyboard.ingest(self.command(update_id=101 + len(self.calls)))
                self.responses.append(response)
                await self.keyboard.handle_one()
                self.assertEqual(self.jobs()[-1]['state'], 'uncertain')
                self.assertFalse(await self.keyboard.handle_one())

    async def test_429_retries_same_intent_only_after_the_server_delay(self):
        self.keyboard.ensure_startup()
        self.responses.append(TelegramError('sendMessage', 429, 'limited', retry_after=12))
        await self.keyboard.handle_one()
        self.assertEqual(self.jobs()[0]['state'], 'pending')
        self.assertEqual(self.jobs()[0]['retry_at'], self.now + 12)
        self.assertFalse(await self.keyboard.handle_one())
        self.now += 12
        self.assertTrue(await self.keyboard.handle_one())
        self.assertEqual(self.jobs()[0]['state'], 'sent')
        self.assertEqual(self.jobs()[0]['attempts'], 2)
        self.assertEqual(len(self.calls), 2)

    async def test_recovery_of_a_claimed_send_is_uncertain_and_never_automatically_reissued(self):
        self.keyboard.ensure_startup()
        job = self.jobs()[0]
        self.keyboard._finish(job, 'sending')
        restarted = QuickKeyboard(self.router, clock=lambda: self.now)
        restarted.recover()
        self.assertEqual(self.jobs()[0]['state'], 'uncertain')
        self.assertEqual(restarted.state()['state'], 'uncertain')
        self.assertFalse(restarted.ensure_startup())
        self.assertFalse(await restarted.handle_one())
        self.assertFalse(self.calls)

    async def test_permanent_rejection_does_not_spin_or_end_the_bridge(self):
        self.keyboard.ensure_startup()
        self.responses.append(TelegramError('sendMessage', 403, 'forbidden'))
        self.assertTrue(await self.keyboard.handle_one())
        self.assertEqual(self.jobs()[0]['state'], 'failed')
        self.assertFalse(self.keyboard.ensure_startup())
        self.assertFalse(await self.keyboard.handle_one())
        self.assertEqual(len(self.calls), 1)

    async def test_pending_old_identity_is_not_sent_to_a_different_bot(self):
        self.keyboard.ensure_startup()
        old_fingerprint = self.jobs()[0]['fingerprint']
        self.router.bot_id = 555
        self.assertTrue(self.keyboard.ensure_startup())
        # Return the new exact Bot identity from its own transport.
        self.responses.append({'message_id': 200, 'from': {'id': 555}, 'chat': {'id': 123}})
        await self.keyboard.handle_one()
        self.assertEqual(self.jobs()[0]['state'], 'pending')
        self.assertEqual(self.jobs()[1]['state'], 'sent')
        self.assertNotEqual(self.keyboard.state()['fingerprint'], old_fingerprint)

    async def test_concurrent_workers_cannot_duplicate_the_installation(self):
        self.keyboard.ensure_startup()
        self.assertEqual(await asyncio.gather(self.keyboard.handle_one(), self.keyboard.handle_one()), [True, False])
        self.assertEqual(len(self.calls), 1)
