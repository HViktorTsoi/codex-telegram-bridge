import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bridge import TelegramError
import test_main_guard as fixtures
from topics_bridge import TopicRouter


class MainGuardNewSessionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.MainGuardTests.asyncTearDown
    callback = fixtures.MainGuardTests.callback
    click = fixtures.MainGuardTests.click
    inbox = fixtures.MainGuardTests.inbox
    message = fixtures.MainGuardTests.message
    pump = fixtures.MainGuardTests.pump
    send = fixtures.MainGuardTests.send

    async def asyncSetUp(self):
        await fixtures.MainGuardTests.asyncSetUp(self)
        self.created_threads = {}
        self.start_error = None
        original_call = self.router.catalog_call

        async def native(method, params):
            if method == 'thread/start':
                self.native_calls.append((method, dict(params)))
                tid = 'created-' + str(len(self.created_threads) + 1)
                thread = {'id': tid, 'cwd': params['cwd'], 'name': None, 'ephemeral': False}
                self.created_threads[tid] = thread
                if self.start_error:
                    raise self.start_error  # The session exists even if the response was lost.
                return {'thread': dict(thread)}
            if method == 'thread/name/set':
                self.native_calls.append((method, dict(params)))
                self.created_threads[params['threadId']]['name'] = params['name']
                return {}
            if method == 'thread/read' and params['threadId'] in self.created_threads:
                self.native_calls.append((method, dict(params)))
                return {'thread': dict(self.created_threads[params['threadId']])}
            return await original_call(method, params)

        async def verify(tid):
            return (await native('thread/read', {'threadId': tid}))['thread']

        self.router.catalog_call = native
        self.router.verify_existing = verify

    def new_target(self):
        targets = [target for target in self.router.targets.values() if target['thread_id'] in self.created_threads]
        self.assertEqual(len(targets), 1)
        return targets[0]

    def attempts(self):
        return (sum(method == 'thread/start' for method, _ in self.native_calls),
                sum(method == 'createForumTopic' for method, _ in self.telegram.calls))

    async def test_one_click_creates_in_home_and_queues_only_the_chosen_message(self):
        await self.send('之前的消息仍在待发送区')
        attachment = {'file_id': 'first-attachment', 'file_name': '报告.txt'}
        update, panel = await self.send('', caption='按照附件整理报告', document=attachment)
        self.assertEqual(self.attempts(), (0, 0))
        self.assertIn('Instagram', str(panel['reply_markup']))

        result = await self.click(panel, '新建对话')

        self.assertEqual(self.attempts(), (1, 1))
        target = self.new_target()
        self.assertTrue(target['name'].startswith('新会话 '))
        self.assertEqual(self.created_threads[target['thread_id']]['cwd'], str(Path.home().resolve()))
        self.assertEqual(self.created_threads[target['thread_id']]['name'], target['name'])
        self.assertEqual(self.telegram.created, [{'chat_id': 123, 'name': target['name']}])
        self.assertIn(target['name'], result['text'])
        self.assertNotIn('确认新建', str(result['reply_markup']))
        self.assertIn(target, json.loads((self.path / 'config.json').read_text())['targets'])
        row, = self.inbox(target['key'])
        self.assertEqual((row['id'], row['text'], row['message_thread_id']),
                         (update['update_id'], '按照附件整理报告', target['message_thread_id']))
        self.assertEqual(json.loads(row['message_json'])['document'], attachment)
        self.assertEqual(json.loads(row['main_guard_json'])['thread_id'], target['thread_id'])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.inbox('instagram'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_duplicate_create_and_competing_route_clicks_only_queue_once(self):
        original, panel = await self.send('只交给新会话一次')
        for label in ('新建对话', '新建对话', 'Instagram'):
            self.router.ingest(self.callback(panel, label))
        await self.pump()

        self.assertEqual(self.attempts(), (1, 1))
        row, = self.inbox(self.new_target()['key'])
        self.assertEqual(row['id'], original['update_id'])
        self.assertEqual(self.inbox('instagram'), [])

    async def test_stale_album_button_cannot_create_before_selecting_on_the_updated_panel(self):
        first = self.message('', caption='按顺序整理这些照片', media_group_id='album',
                             photo=[{'file_id': 'photo-1'}])
        self.router.ingest(first)
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]

        second = self.message('', media_group_id='album', photo=[{'file_id': 'photo-2'}])
        self.router.ingest(second)
        self.router.ingest(self.callback(panel, '新建对话'))
        self.now += 3
        await self.pump()
        self.assertEqual(self.attempts(), (0, 0))

        await self.click(self.telegram.messages[panel['message_id']], '新建对话')

        self.assertEqual(self.attempts(), (1, 1))
        rows = self.inbox(self.new_target()['key'])
        self.assertEqual([row['id'] for row in rows], [first['update_id'], second['update_id']])
        self.assertEqual([row['media_group_id'] for row in rows], ['album', 'album'])
        self.assertEqual([json.loads(row['message_json'])['photo'][0]['file_id'] for row in rows],
                         ['photo-1', 'photo-2'])

    async def test_known_topic_failure_retries_using_the_same_native_thread(self):
        original, panel = await self.send('失败后仍要发送这条消息')
        self.telegram.create_error = TelegramError('createForumTopic', 400, 'Bad Request: invalid name')
        retry = await self.click(panel, '新建对话')
        self.assertEqual(self.attempts(), (1, 1))
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.inbox('instagram'), [])

        self.telegram.create_error = None
        await self.click(retry, '重试')

        self.assertEqual(self.attempts(), (1, 2))
        row, = self.inbox(self.new_target()['key'])
        self.assertEqual(row['id'], original['update_id'])

    async def test_lost_native_creation_receipt_cannot_start_a_second_session(self):
        _, panel = await self.send('有待核对的创建结果')
        self.start_error = asyncio.TimeoutError()
        retry = await self.click(panel, '新建对话')
        await self.router.picker.recover()
        self.start_error = None
        await self.click(retry, '重试')

        self.assertEqual(self.attempts(), (1, 0))
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.inbox('instagram'), [])

    async def test_topic_install_retry_reuses_the_acknowledged_topic(self):
        original, panel = await self.send('用同一话题恢复投递')
        with patch.object(self.router, 'install_target', side_effect=OSError('disk unavailable')):
            retry = await self.click(panel, '新建对话')
        self.assertEqual(self.attempts(), (1, 1))
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

        await self.click(retry, '重试')

        self.assertEqual(self.attempts(), (1, 1))
        target = self.new_target()
        row, = self.inbox(target['key'])
        self.assertEqual((row['id'], row['message_thread_id']), (original['update_id'], 901))

    async def test_restart_with_recorded_binding_finishes_the_original_delivery(self):
        original, panel = await self.send('绑定成功后重启，继续这条任务')
        creator = self.router.picker.creator
        create = creator.create_for_all

        async def interrupted(*args, **kwargs):
            await create(*args, **kwargs)
            raise asyncio.CancelledError()  # Stop before recording the target in the input draft.

        with patch.object(creator, 'create_for_all', side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.click(panel, '新建对话')
        target = self.new_target()
        self.assertEqual(self.inbox(target['key']), [])
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        config = json.loads((self.path / 'config.json').read_text())
        self.router = TopicRouter(config, self.path)
        self.router.bot_id, self.router.telegram = 444, self.telegram

        await self.router.picker.recover()
        await self.pump()

        self.assertEqual(self.attempts(), (1, 1))
        row, = self.inbox(target['key'])
        self.assertEqual((row['id'], row['text']), (original['update_id'], '绑定成功后重启，继续这条任务'))
