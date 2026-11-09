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

    async def test_verified_bot_and_frozen_topic_are_separate_from_user_request(self):
        row = self.accept()
        await self.bridge.dispatch_one(row)
        method, params = self.bridge.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'shared-native'))
        self.assertEqual(params['input'][0], {'type': 'text', 'text': '文件发给我'})
        origin = params['input'][-1]['text']
        self.assertIn('@hvt_claude_zbox_bot', origin)
        self.assertIn('6305', origin)
        self.assertNotIn('@hvt_zbox_codex_bot', origin)
        self.assertIn('不属于用户正文', origin)
        self.assertIn('Markdown 链接', origin)
        self.assertIn('不要沿用历史中的其他 Bot 配置', origin)
        self.assertNotIn('/.claude/', origin)
        self.assertEqual(self.bridge.store.db.execute('SELECT text FROM inbox').fetchone()[0], '文件发给我')

    async def test_two_bots_sharing_native_session_get_their_actual_origin(self):
        await self.bridge.dispatch_one(self.accept())
        primary = self.bridge.codex.call.call_args.args[1]['input'][-1]['text']
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
            origin = params['input'][-1]['text']
            self.assertIn('@hvt_zbox_codex_bot', origin)
            self.assertIn('主对话', origin)
            self.assertNotIn('@hvt_claude_zbox_bot', origin)
            self.assertNotEqual(primary, origin)
        finally:
            secondary.store.db.close()

    async def test_unverified_runtime_does_not_guess_origin_from_old_configuration(self):
        self.bridge.username = None
        self.bridge.config['expected_bot_username'] = 'old_history_bot'
        await self.bridge.dispatch_one(self.accept('普通任务'))
        self.assertEqual(self.bridge.codex.call.call_args.args[1]['input'], [{'type': 'text', 'text': '普通任务'}])

    async def test_multimodal_input_and_quote_are_preserved_before_route_context(self):
        row = self.accept('处理引用中的新附件', photo=[{'file_id': 'photo', 'file_size': 20}])
        contents = [{'type': 'text', 'text': '处理引用中的新附件\n\n［引用内容］\n> 原文'},
                    {'type': 'localImage', 'path': '/private/inbox/photo.jpg'}]
        with patch('bridge.inputs_for', AsyncMock(return_value=copy.deepcopy(contents))):
            await self.bridge.dispatch_one(row)
        supplied = self.bridge.codex.call.call_args.args[1]['input']
        self.assertEqual(supplied[:2], contents)
        self.assertIn('［Telegram 回传路由］', supplied[2]['text'])

    async def test_system_command_and_changed_guard_route_do_not_start_model_or_add_context(self):
        await self.bridge.dispatch_one(self.accept('/not-a-command'))
        self.bridge.codex.call.assert_not_awaited()
        with self.bridge.store.db:
            binding = json.dumps({'target_key': 'manager', 'thread_id': 'shared-native', 'topic_id': 6305})
            self.bridge.store.db.execute("UPDATE inbox SET state='pending',text='发送文件',message_thread_id=6306,main_guard_json=?", (binding,))
        await self.bridge.dispatch_one(self.bridge.store.pending())
        self.bridge.codex.call.assert_not_awaited()
