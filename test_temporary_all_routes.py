import copy
import json
import time
import unittest
from unittest.mock import AsyncMock

import test_session_picker as fixtures
from topics_bridge import TargetBridge


class TemporaryAllRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.PickerTests.asyncSetUp(self)
        self.now = time.time()
        self.router.picker.guard.clock = lambda: self.now
        control = self.path / 'control'
        control.mkdir(exist_ok=True)
        entries = [{'host': 'zbox', 'thread_id': t['thread_id'], 'name': t['name'],
                    'target_key': t['key'], 'topic_id': t['message_thread_id'],
                    'aliases': [t['key'], t['name']], 'status': {'type': 'idle'},
                    'questions': [], 'updated': self.now} for t in self.config['targets']]
        (control / 'local.json').write_text(json.dumps({'host': 'zbox', 'generated': self.now, 'entries': entries}))
        target = await self.router.picker.add_temporary('new-native')
        self.temp_key = target['key']
        self.temp_bridge = self.router.temporary_bridges[self.temp_key]

    async def asyncTearDown(self):
        for bridge in self.router.temporary_bridges.values():
            bridge.store.db.close()
        await fixtures.PickerTests.asyncTearDown(self)

    def message(self, text='普通 All 消息', topic=None, sender=123, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': sender},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, 'date': 1}
        if topic:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': self.sequence, 'message': message}

    async def send(self, text='普通 All 消息', **extra):
        update = self.message(text, **extra)
        self.router.ingest(update)
        await self.pump()
        return update, self.telegram.messages.get(self.telegram.next_id)

    async def pump(self):
        for _ in range(50):
            if not await self.router.picker.handle_one():
                return
        self.fail('Management job loop failed to become idle')

    async def click(self, message, label, **kwargs):
        self.router.ingest(self.callback(message, label, **kwargs))
        await self.pump()
        return self.telegram.messages[message['message_id']]

    async def menu(self, topic_id=11):
        self.sequence += 1
        msg = {'message_id': self.sequence, 'from': {'id': 123},
               'chat': {'id': 123, 'type': 'private'}, 'text': '/sessions'}
        if topic_id:
            msg['message_thread_id'] = topic_id
        self.router.ingest({'update_id': self.sequence, 'message': msg})
        await self.router.picker.handle_one()
        return self.telegram.messages[self.telegram.next_id]

    def callback(self, message, label, sender=123):
        button = next(b for row in message['reply_markup']['inline_keyboard'] for b in row if label in b['text'])
        self.sequence += 1
        return {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence), 'from': {'id': sender},
                'message': copy.deepcopy(message), 'data': button['callback_data']}}

    async def test_added_route_is_enabled_and_persisted_without_topic_creation(self):
        menu = await self.menu()
        self.assertIn('临时 All 路由', menu['text'])
        self.assertIn(f'{len(self.router.targets) + 1}. 新会话 【临时】', menu['text'])
        panel = await self.click(menu, '新会话 【临时】')
        self.assertIn('修改名称', str(panel['reply_markup']))
        self.assertIn('关闭临时路由', str(panel['reply_markup']))
        self.assertIn('已开启', panel['text'])
        self.assertEqual(self.telegram.created, [])
        saved = json.loads((self.path / 'config.json').read_text())
        entry = next(item for item in saved['temporary_all_routes'] if item['key'] == self.temp_key)
        self.assertTrue(entry['enabled'])

    async def test_added_route_is_available_in_all_and_keeps_none_topic(self):
        menu = await self.menu()
        panel = await self.click(menu, '新会话 【临时】')
        _, panel = await self.send('发给临时会话')
        self.assertIn('新会话', str(panel['reply_markup']))
        await self.click(panel, '新会话')
        row = self.temp_bridge.store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual(row['message_thread_id'], None)
        self.assertEqual(row['text'], '发给临时会话')
        self.assertNotIn('message_thread_id', json.loads(row['message_json']))
        self.assertIn('临时 All 队列', self.telegram.messages[self.telegram.next_id]['text'])

    async def test_replies_are_labeled_in_all(self):
        self.temp_bridge.store.enqueue_at('turn:turn-1:item-1', '123', 'Codex 临时回复', None)
        row = self.temp_bridge.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['message_thread_id'], None)
        self.assertTrue(row['text'].startswith('**新会话 【临时】**\n\n'))

    async def test_temporary_route_submenu_renames_native_session_and_label_without_topic_api(self):
        native = {'id': 'new-native', 'name': '新会话', 'cwd': '/work/new', 'ephemeral': False}

        async def catalog(method, params):
            if method == 'thread/read':
                return {'thread': dict(native)}
            if method == 'thread/name/set':
                native['name'] = params['name']
                return {}
            raise AssertionError(method)

        self.router.catalog_call = AsyncMock(side_effect=catalog)
        menu = await self.menu()
        panel = await self.click(menu, '新会话 【临时】')
        await self.click(panel, '修改名称')
        prompt = max((m for m in self.telegram.messages.values()
                      if m.get('reply_markup', {}).get('force_reply')), key=lambda m: m['message_id'])
        update = self.message('ReID新名', topic=None, reply_to_message=copy.deepcopy(prompt))
        self.router.ingest(update)
        await self.pump()
        review = max((m for m in self.telegram.messages.values()
                      if '确认改名？' in m.get('text', '')), key=lambda m: m['message_id'])
        self.assertIn('临时 All 标签', review['text'])
        await self.click(review, '确认改名')
        target = self.router.temporary_routes[self.temp_key]
        self.assertEqual(native['name'], 'ReID新名')
        self.assertEqual(target['name'], 'ReID新名')
        self.assertEqual(target['all_route_label'], 'ReID新名 【临时】')
        self.assertEqual(self.temp_bridge.store.output_label, 'ReID新名 【临时】')
        self.assertFalse(any(method == 'editForumTopic' for method, _ in self.telegram.calls))

    async def test_temporary_route_can_be_cancelled_without_deleting_native_session(self):
        menu = await self.menu()
        panel = await self.click(menu, '新会话 【临时】')
        confirm = await self.click(panel, '取消临时绑定')
        self.assertIn('确认取消临时绑定', confirm['text'])
        result = await self.click(confirm, '确认取消临时绑定')
        self.assertIn('已取消', result['text'])
        self.assertNotIn(self.temp_key, self.router.temporary_routes)
        saved = json.loads((self.path / 'config.json').read_text())
        self.assertEqual(saved['temporary_all_routes'], [])
        self.assertNotIn('createForumTopic', [method for method, _ in self.telegram.calls])
        restored = await self.router.picker.add_temporary('new-native')
        self.assertEqual(restored['thread_id'], 'new-native')

    async def test_cancel_accepts_legacy_route_without_persisted_label(self):
        config_path = self.path / 'config.json'
        saved = json.loads(config_path.read_text())
        saved['temporary_all_routes'][0].pop('label', None)
        config_path.write_text(json.dumps(saved))
        menu = await self.menu()
        panel = await self.click(menu, '新会话 【临时】')
        confirm = await self.click(panel, '取消临时绑定')
        result = await self.click(confirm, '确认取消临时绑定')
        self.assertIn('已取消', result['text'])
        self.assertEqual(json.loads(config_path.read_text())['temporary_all_routes'], [])

    async def test_sessions_can_add_an_arbitrary_existing_session_as_temporary_route(self):
        menu = await self.menu()
        directories = await self.click(menu, '添加临时 All')
        self.assertIn('添加临时 All 路由', directories['text'])
        catalog = await self.click(directories, '1. 📁 ')
        confirmation = await self.click(catalog, '第二页会话')
        self.assertIn('不会创建 Telegram 话题', confirmation['text'])
        result = await self.click(confirmation, '确认添加临时路由')
        self.assertIn('临时 All 路由已存在', result['text'])
        self.assertEqual(len(self.router.temporary_routes), 2)
        self.assertEqual(self.telegram.created, [])


if __name__ == '__main__':
    unittest.main()
