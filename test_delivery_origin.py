import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge import Bridge
from topics_bridge import TargetBridge


class DeliveryOriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'owner_id': '123', 'thread_id': 'shared-native', 'socket_path': '/unused',
                       'telegram_state_dir': str(self.path), 'notification_mode': 'completion'}
        self.target = {'key': 'manager', 'name': 'Manager', 'thread_id': 'shared-native', 'message_thread_id': 6305}
        self.bridge = TargetBridge(self.config, self.path, self.target, lambda: '')
        self.bridge.username = 'hvt_claude_zbox_bot'
        self.bridge.codex = SimpleNamespace(call=AsyncMock(return_value={'turn': {'id': 'task'}}))
        self.bridge.telegram = SimpleNamespace()

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def accept(self, text='文件发给我', **extra):
        message = {'message_id': 7, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_thread_id': 6305, 'text': text, **extra}
        self.bridge.store.accept({'update_id': 7}, message)
        return self.bridge.store.pending()

    async def test_verified_bot_and_topic_do_not_add_text_to_user_input(self):
        text = '文件发给我\n\n请保留这段原文。'
        row = self.accept(text)
        await self.bridge.dispatch_one(row)
        method, params = self.bridge.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'shared-native'))
        self.assertEqual(params['input'], [{'type': 'text', 'text': text}])
        self.assertEqual(self.bridge.store.db.execute('SELECT text FROM inbox').fetchone()[0], text)

    async def test_two_bots_sharing_native_session_preserve_each_original_input(self):
        await self.bridge.dispatch_one(self.accept())
        primary = self.bridge.codex.call.call_args.args[1]['input']
        self.assertEqual(primary, [{'type': 'text', 'text': '文件发给我'}])
        other_path = self.path / 'secondary'
        other_path.mkdir()
        secondary = Bridge(self.config, other_path)
        try:
            secondary.username = 'hvt_zbox_codex_bot'
            secondary.codex = SimpleNamespace(call=AsyncMock(return_value={'turn': {'id': 'secondary-task'}}))
            secondary.telegram = SimpleNamespace()
            secondary.store.accept({'update_id': 8}, {'message_id': 8, 'from': {'id': 123},
                                                     'chat': {'id': 123, 'type': 'private'}, 'text': '发送脚本'})
            await secondary.dispatch_one(secondary.store.pending())
            params = secondary.codex.call.call_args.args[1]
            self.assertEqual(params['threadId'], self.bridge.thread_id)
            self.assertEqual(params['input'], [{'type': 'text', 'text': '发送脚本'}])
        finally:
            secondary.store.db.close()

    async def test_missing_runtime_identity_also_leaves_user_input_unchanged(self):
        self.bridge.username = None
        self.bridge.config['expected_bot_username'] = 'old_history_bot'
        await self.bridge.dispatch_one(self.accept('普通任务'))
        self.assertEqual(self.bridge.codex.call.call_args.args[1]['input'], [{'type': 'text', 'text': '普通任务'}])

    async def test_multimodal_quote_and_real_attachment_metadata_are_preserved_without_extra_context(self):
        row = self.accept('处理引用中的新附件', photo=[{'file_id': 'photo', 'file_size': 20}])
        text = ('处理引用中的新附件\n\n［引用内容］\n> 原文\n\n'
                'Telegram附件已保存至本机，以下是文件元数据（JSON）：\n'
                '[{"kind":"photo","path":"/private/inbox/photo.jpg","mime_type":"image/jpeg"}]')
        contents = [{'type': 'text', 'text': text},
                    {'type': 'localImage', 'path': '/private/inbox/photo.jpg'}]
        with patch('bridge.inputs_for', AsyncMock(return_value=copy.deepcopy(contents))):
            await self.bridge.dispatch_one(row)
        supplied = self.bridge.codex.call.call_args.args[1]['input']
        self.assertEqual(supplied, contents)

    async def test_system_command_and_changed_guard_route_do_not_start_model(self):
        await self.bridge.dispatch_one(self.accept('/not-a-command'))
        self.bridge.codex.call.assert_not_awaited()
        with self.bridge.store.db:
            binding = json.dumps({'target_key': 'manager', 'thread_id': 'shared-native', 'topic_id': 6305})
            self.bridge.store.db.execute("UPDATE inbox SET state='pending',text='发送文件',message_thread_id=6306,main_guard_json=?", (binding,))
        await self.bridge.dispatch_one(self.bridge.store.pending())
        self.bridge.codex.call.assert_not_awaited()
