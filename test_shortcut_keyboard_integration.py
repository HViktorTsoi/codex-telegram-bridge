"""Keyboard labels reuse existing command lanes and preserve prompt input."""
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock

import test_main_guard as fixtures


class ShortcutKeyboardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MainGuardTests.asyncSetUp
    asyncTearDown = fixtures.MainGuardTests.asyncTearDown
    message = fixtures.MainGuardTests.message
    pump = fixtures.MainGuardTests.pump
    inbox = fixtures.MainGuardTests.inbox

    async def test_session_button_opens_the_existing_main_menu_without_a_model_turn(self):
        update = self.message('📚 会话', topic=22)
        original = copy.deepcopy(update)
        self.router.ingest(update)
        await self.pump()
        self.assertEqual(update, original)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertTrue(self.telegram.messages)
        self.assertTrue(all(not m.get('message_thread_id') for m in self.telegram.messages.values()))
        self.assertFalse(any(self.inbox(key) for key in self.router.bridges))
        self.assertEqual(self.native_calls, [])

    async def test_task_and_inbox_buttons_keep_the_global_command_lane(self):
        for label, command in [('📋 任务', '/tasks'), ('📥 待处理', '/inbox')]:
            update = self.message(label, topic=22)
            self.router.ingest(update)
            row = self.router.store.db.execute('SELECT payload FROM cc_jobs WHERE id=?', (update['update_id'],)).fetchone()
            self.assertEqual(json.loads(row['payload'])['message']['text'], command)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertFalse(any(self.inbox(key) for key in self.router.bridges))
        self.assertEqual(self.native_calls, [])

    async def test_model_and_effort_buttons_freeze_the_current_topic_not_the_keyboard_publish_location(self):
        for label, command in [('🤖 模型', '/model'), ('🧠 强度', '/effort')]:
            update = self.message(label, topic=22)
            self.router.ingest(update)
            row = self.router.store.db.execute('SELECT * FROM settings_jobs WHERE id=?', (update['update_id'],)).fetchone()
            self.assertEqual(row['target_key'], 'instagram')
            payload = json.loads(row['payload'])['message']
            self.assertEqual((payload['text'], payload['message_thread_id']), (command, 22))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.native_calls, [])

    async def test_skills_button_uses_the_current_topic_skills_menu(self):
        update = self.message('🧩 技能', topic=22)
        self.router.ingest(update)
        row = self.router.store.db.execute('SELECT payload FROM skills_jobs WHERE id=?', (update['update_id'],)).fetchone()
        message = json.loads(row['payload'])['message']
        self.assertEqual((message['text'], message['message_thread_id']), ('/skills', 22))
        self.assertFalse(any(self.inbox(key) for key in self.router.bridges))
        self.assertEqual(self.native_calls, [])

    async def test_stop_button_only_interrupts_the_current_native_thread(self):
        update = self.message('⏹ 停止', topic=22)
        self.router.ingest(update)
        while self.router.forward_one():
            pass
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {
            'turns': AsyncMock(return_value=[{'id': 'current', 'status': 'inProgress'}]),
            'call': AsyncMock(return_value={})})()
        row = child.store.pending()
        self.assertEqual(row['text'], '/stop')
        await child.dispatch_one(row)
        child.codex.call.assert_awaited_once_with('turn/interrupt', {'threadId': 'instagram-native', 'turnId': 'current'})
        self.assertEqual(self.inbox('meeting'), [])

    async def test_help_button_does_not_enter_the_all_message_selector(self):
        update = self.message('❓ 帮助')
        self.router.ingest(update)
        while self.router.forward_one():
            pass
        child = self.router.bridges['meeting']
        self.assertEqual(child.store.pending()['text'], '/help')
        await child.dispatch_one(child.store.pending())
        outbox = child.store.db.execute('SELECT text,message_thread_id FROM outbox').fetchall()
        self.assertTrue(outbox)
        self.assertTrue(all(row['message_thread_id'] is None for row in outbox))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.native_calls, [])

    async def test_new_session_and_rename_prompt_data_are_not_converted_to_commands(self):
        from session_creator import PROMPT_TITLE as NEW_TITLE
        from session_renamer import PROMPT_TITLE as RENAME_TITLE
        for title in (NEW_TITLE, RENAME_TITLE):
            update = self.message('🤖 模型', reply_to_message={
                'message_id': 90, 'from': {'id': 444}, 'text': title})
            self.router.ingest(update)
            row = self.router.store.db.execute('SELECT payload FROM ui_jobs WHERE id=?', (update['update_id'],)).fetchone()
            self.assertEqual(json.loads(row['payload'])['message']['text'], '🤖 模型')
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM settings_jobs').fetchone()[0], 0)

    async def test_skills_choice_and_dispatch_prompt_replies_keep_literal_button_labels(self):
        from skills_menu import PROMPT_TITLE
        update = self.message('🧠 强度', topic=22, reply_to_message={
            'message_id': 91, 'from': {'id': 444}, 'text': PROMPT_TITLE})
        self.router.ingest(update)
        row = self.router.store.db.execute('SELECT payload FROM skills_jobs WHERE id=?', (update['update_id'],)).fetchone()
        self.assertEqual(json.loads(row['payload'])['message']['text'], '🧠 强度')
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO choice_prompts(chat_id,message_id,topic_id,bot_id,question_id,position) '
                'VALUES(?,?,?,?,?,?)', ('123', 92, 22, 444, 'question', 0))
            self.router.store.db.execute('INSERT INTO cc_prompts VALUES(?,?,?,?,?)',
                (93, '123', 22, '{}', time.time() + 900))
        for parent_id, table in [(92, 'choice_jobs'), (93, 'cc_jobs')]:
            update = self.message('🧠 强度', topic=22, reply_to_message={
                'message_id': parent_id, 'from': {'id': 444}, 'text': '输入回复'})
            self.router.ingest(update)
            row = self.router.store.db.execute('SELECT payload FROM ' + table + ' WHERE id=?', (update['update_id'],)).fetchone()
            self.assertEqual(json.loads(row['payload'])['message']['text'], '🧠 强度')
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM settings_jobs').fetchone()[0], 0)

    async def test_attachment_captions_and_unauthorized_labels_remain_outside_shortcut_commands(self):
        update = self.message('', photo=[{'file_id': 'new-photo'}], caption='⏹ 停止')
        self.router.ingest(update)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        update = self.message('⏹ 停止', topic=22, sender=999)
        self.router.ingest(update)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertFalse(any(self.inbox(key) for key in self.router.bridges))

    async def test_unbound_topic_cannot_use_keyboard_to_reach_a_default_native_session(self):
        update = self.message('⏹ 停止', topic=999)
        self.router.ingest(update)
        while self.router.forward_one():
            pass
        self.assertFalse(any(self.inbox(key) for key in self.router.bridges))
        notice = self.router.store.db.execute('SELECT * FROM notices').fetchone()
        self.assertEqual(notice['topic_id'], 999)
