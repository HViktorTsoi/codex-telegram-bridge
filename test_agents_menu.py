"""Read-only agent panels retain their route and delivery evidence."""
import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents_catalog import AgentCatalogError
from agents_menu import AgentsMenu
from bridge import TelegramError
from message_format import utf16_size


class AgentsMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT INTO kv VALUES('offset','0')")
        store = SimpleNamespace(db=self.db, offset=lambda: int(self.db.execute(
            "SELECT value FROM kv WHERE key='offset'").fetchone()[0]))
        self.router = SimpleNamespace(store=store, owner='123', bot_id=444, username='test_bot',
            config={'expected_bot_username': 'test_bot'}, targets={}, bridges={}, by_topic={})
        self.access = {'dmPolicy': 'allowlist', 'allowFrom': ['123']}
        self.router.access = lambda: self.access
        self.router.owner_allowed = lambda: True
        self.denied = set()
        self.router.thread_allowed = lambda root: root not in self.denied
        for key, topic in [('a', 42), ('b', 84)]:
            target = {'key': key, 'name': '会话 ' + key.upper(), 'thread_id': 'native-' + key,
                      'message_thread_id': topic}
            self.router.targets[key] = self.router.by_topic[topic] = target
            self.router.bridges[key] = SimpleNamespace(target=target, thread_id=target['thread_id'],
                owner_allowed=lambda: True, store=SimpleNamespace(get=lambda k, tid=target['thread_id']: tid))
        self.clock, self.counter = 10000, 100
        self.calls, self.catalog_calls, self.messages, self.failures = [], [], {}, {}
        self.catalog_error, self.catalog_hook, self.send_hook = None, None, None
        self.ack = 'normal'
        self.rows = {root: [self.agent(root, i) for i in range(15)] for root in ('native-a', 'native-b')}
        test = self

        class Catalog:
            async def roots(self, **kwargs):
                raise AssertionError('The topic browser must never enumerate roots')

            async def agents(self, root_id, *, refresh=False):
                test.catalog_calls.append(('agents', root_id, refresh))
                if test.catalog_hook:
                    await test.catalog_hook(root_id)
                if test.catalog_error:
                    raise test.catalog_error
                return copy.deepcopy(test.rows[root_id])

            async def detail(self, root_id, agent_id):
                test.catalog_calls.append(('detail', root_id, agent_id))
                row = next(a for a in test.rows[root_id] if a['id'] == agent_id)
                return {**copy.deepcopy(row), 'root_id': root_id,
                    'task': '任务：查看 /tmp/source.pdf 并返回结果。',
                    'latest_reply': '结果： [report](/tmp/out.pdf)\n{"path":"/tmp/file.png"}',
                    'last_turn_status': 'completed'}

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                failures = test.failures.get(method, [])
                if failures:
                    raise failures.pop(0)
                if method == 'answerCallbackQuery':
                    return True
                if method not in ('sendMessage', 'editMessageText'):
                    raise AssertionError('Read-only panels may only send/edit text: ' + method)
                if test.ack != 'normal':
                    return test.ack
                if method == 'editMessageText':
                    result = {**test.messages[params['message_id']], **copy.deepcopy(params)}
                else:
                    test.counter += 1
                    result = {**copy.deepcopy(params), 'message_id': test.counter, 'date': 1,
                        'from': {'id': 444, 'is_bot': True}, 'chat': {'id': 123, 'type': 'private'}}
                test.messages[result['message_id']] = copy.deepcopy(result)
                if test.send_hook:
                    await test.send_hook()
                return copy.deepcopy(result)

        self.router.telegram = Telegram()
        self.ui = AgentsMenu(self.router, clock=lambda: self.clock)
        self.ui.catalog = Catalog()

    async def asyncTearDown(self):
        self.db.close()

    @staticmethod
    def agent(root, index):
        return {'id': f'{root}-child-{index}', 'name': f'agent_{index}',
            'parent_id': root if index == 0 else f'{root}-child-{index - 1}',
            'role': 'worker', 'status': 'active' if index % 4 == 0 else 'idle',
            'archived': index == 14, 'cwd': '/home/project', 'updated_at': 1000 + index,
            'preview': f'public preview {index}', 'depth': index + 1}

    def update(self, text='/agents', topic=42, sender=123, **extra):
        self.counter += 1
        message = {'message_id': self.counter, 'date': 1, 'text': text,
                   'chat': {'id': 123, 'type': 'private'}, 'from': {'id': sender}, **extra}
        if topic is not None:
            message['message_thread_id'] = topic
        return {'update_id': self.counter, 'message': message}

    async def open(self, topic=42):
        update = self.update(topic=topic)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return self.panel()

    def panel(self):
        return copy.deepcopy(max(self.messages.values(), key=lambda m: m['message_id']))

    def buttons(self, panel=None):
        return [b for row in (panel or self.panel())['reply_markup']['inline_keyboard'] for b in row]

    def click_update(self, panel, name, **changes):
        button = next(b for b in self.buttons(panel) if b['text'] == name or name in b['text'])
        self.counter += 1
        return {'update_id': self.counter, 'callback_query': {'id': str(self.counter),
            'from': {'id': 123}, 'data': button['callback_data'],
            'message': {**copy.deepcopy(panel), **changes}}}

    async def click(self, panel, name, **changes):
        update = self.click_update(panel, name, **changes)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return self.panel()

    def state(self, table='ag_jobs'):
        return dict(self.db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1').fetchone())

    def deliveries(self):
        return [(m, p) for m, p in self.calls if m in ('sendMessage', 'editMessageText')]

    async def test_topic_opens_all_descendants_without_enumerating_other_sessions(self):
        panel = await self.open()
        self.assertIn('会话 A', panel['text'])
        self.assertIn('共 15 个子代理', panel['text'])
        self.assertIn('第 1/3 页', panel['text'])
        self.assertIn('含历史与归档', panel['text'])
        self.assertEqual(self.catalog_calls, [('agents', 'native-a', False)])
        self.assertEqual((panel['chat_id'], panel['message_thread_id']), ('123', 42))
        self.assertNotIn('回目录', str(panel))
        self.assertTrue(all(len(b['callback_data']) < 64 for b in self.buttons(panel)))

    async def test_all_and_unbound_topic_only_explain_where_to_open(self):
        for topic in (None, 99):
            update = self.update(topic=topic)
            self.assertTrue(self.ui.ingest(update))
            await self.ui.handle_one()
            result = self.panel()
            self.assertIn('已绑定话题', result['text'])
            self.assertEqual(result.get('message_thread_id'), topic)
        self.assertEqual(self.catalog_calls, [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ag_panels').fetchone()[0], 0)

    async def test_complete_local_pagination_includes_deep_archived_agent(self):
        first = await self.open()
        second = await self.click(first, '下一页')
        last = await self.click(second, '下一页')
        self.assertEqual(first['message_id'], last['message_id'])
        self.assertIn('agent_14', last['text'])
        self.assertIn('层级 15 · 已归档', last['text'])
        self.assertEqual([m for m, _ in self.deliveries()], ['sendMessage', 'editMessageText', 'editMessageText'])
        self.assertEqual(self.state('ag_panels')['page'], 2)

    async def test_active_filter_resets_page_and_all_restores_idle_and_history(self):
        panel = await self.click(await self.open(), '下一页')
        active = await self.click(panel, '工作中')
        self.assertIn('共 4 个子代理', active['text'])
        self.assertIn('第 1/1 页', active['text'])
        self.assertNotIn('空闲', active['text'])
        all_rows = await self.click(active, '全部')
        self.assertIn('共 15 个子代理', all_rows['text'])
        self.assertIn('空闲', all_rows['text'])

    async def test_refresh_edits_same_panel_and_passes_refresh_flag(self):
        panel = await self.open()
        self.rows['native-a'].append(self.agent('native-a', 15))
        refreshed = await self.click(panel, '刷新')
        self.assertEqual(refreshed['message_id'], panel['message_id'])
        self.assertIn('共 16 个子代理', refreshed['text'])
        self.assertEqual(self.catalog_calls[-1], ('agents', 'native-a', True))

    async def test_callback_ack_timeout_continues_render_in_original_binding(self):
        panel = await self.open()
        original_call, original_wait = self.router.telegram.call, asyncio.wait_for

        async def stalled_ack(method, **params):
            if method == 'answerCallbackQuery':
                await asyncio.Event().wait()
            return await original_call(method, **params)

        async def quick_ack_timeout(operation, timeout):
            return await original_wait(operation, .01 if timeout == 5 else timeout)

        self.router.telegram.call = stalled_ack
        with patch('agents_menu.asyncio.wait_for', side_effect=quick_ack_timeout):
            rendered = await self.click(panel, '下一页')
        self.assertEqual(self.state()['state'], 'done')
        self.assertIn('第 2/3 页', rendered['text'])
        self.assertEqual((rendered['message_id'], rendered['chat_id'], rendered['message_thread_id']),
                         (panel['message_id'], panel['chat_id'], panel['message_thread_id']))
        self.assertEqual(self.state('ag_panels')['root_json'], json.dumps(
            {'id': 'native-a', 'name': '会话 A', 'bound_key': 'a', 'topic_id': 42},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        self.assertEqual([m for m, _ in self.deliveries()], ['sendMessage', 'editMessageText'])

    async def test_detail_plain_text_has_relationship_task_reply_and_return_page(self):
        panel = await self.click(await self.open(), '下一页')
        detail = await self.click(panel, 'agent_7')
        for value in ('父代理：native-a-child-6', '层级：8', '任务', '最新公开结果',
                      '/tmp/out.pdf', '工作目录：/home/project', '状态：空闲', '上一轮状态：completed'):
            self.assertIn(value, detail['text'])
        self.assertNotIn('parse_mode', detail)
        self.assertEqual(set(m for m, _ in self.calls), {'sendMessage', 'editMessageText', 'answerCallbackQuery'})
        returned = await self.click(detail, '返回列表')
        self.assertIn('第 2/3 页', returned['text'])
        self.assertEqual(detail['message_id'], returned['message_id'])

    async def test_native_unknown_notloaded_error_and_idle_are_distinct(self):
        self.rows['native-a'] = [self.agent('native-a', i) for i in range(4)]
        for row, state in zip(self.rows['native-a'], ['idle', 'notLoaded', 'systemError', 'unknown']):
            row['status'] = state
        panel = await self.open()
        for value in ('空闲', '未加载', '系统错误', '状态未知'):
            self.assertIn(value, panel['text'])
        self.assertNotIn('已完成', panel['text'])

    async def test_empty_result_is_distinct_from_catalog_failure(self):
        self.rows['native-a'] = []
        panel = await self.open()
        self.assertIn('暂无子代理', panel['text'])
        self.catalog_error = AgentCatalogError('原生代理服务正在连接，请稍后刷新。')
        before = len(self.deliveries())
        await self.click(panel, '刷新')
        self.assertEqual(len(self.deliveries()), before)
        self.assertIn('正在连接', self.calls[-1][1]['text'])
        self.assertTrue(self.calls[-1][1]['show_alert'])

    async def test_initial_catalog_exception_is_clear_and_never_reports_empty(self):
        self.catalog_error = ConnectionError('private transport URL')
        panel = await self.open()
        self.assertIn('未能确认当前状态', panel['text'])
        self.assertNotIn('暂无子代理', panel['text'])
        self.assertNotIn('private transport', panel['text'])
        self.assertEqual(self.state()['state'], 'failed')

    async def test_only_exact_command_and_this_bot_suffix_are_ingested(self):
        for command in ('/agents x', '/agent', 'look /agents', '/agents@other_bot', '/agents\nhello'):
            self.assertFalse(self.ui.ingest(self.update(command)))
        self.assertTrue(self.ui.ingest(self.update('/AGENTS@test_bot')))
        await self.ui.handle_one()
        self.assertEqual(len(self.catalog_calls), 1)

    async def test_attachments_captions_and_forwarded_commands_are_ignored(self):
        for extra in ({'document': {'file_id': 'x'}}, {'photo': []}, {'caption': '/agents'},
                      {'forward_origin': {'type': 'user'}}, {'forward_date': 1},
                      {'forward_from': {'id': 123}}, {'is_automatic_forward': True}):
            self.assertFalse(self.ui.ingest(self.update(**extra)))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ag_jobs').fetchone()[0], 0)

    async def test_nonowner_or_nonallowlist_is_consumed_without_jobs_or_output(self):
        self.assertTrue(self.ui.ingest(self.update(sender=456)))
        self.access['dmPolicy'] = 'open'
        update = self.update()
        self.assertTrue(self.ui.ingest(update))
        self.assertEqual(self.router.store.offset(), update['update_id'] + 1)
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(self.calls, [])

    async def test_identity_mismatch_is_consumed_without_jobs(self):
        self.router.username = 'different_bot'
        self.assertTrue(self.ui.ingest(self.update()))
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(self.calls, [])

    async def test_duplicate_update_advances_offset_once_and_sends_once(self):
        update = self.update()
        self.ui.ingest(update)
        self.ui.ingest(update)
        await self.ui.handle_one()
        self.ui.ingest(update)
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.router.store.offset(), update['update_id'] + 1)

    async def test_offset_and_job_insert_are_one_transaction(self):
        self.db.execute("CREATE TRIGGER reject_agent_job BEFORE INSERT ON ag_jobs BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ui.ingest(self.update())
        self.assertEqual(self.router.store.offset(), 0)

    async def test_callback_is_bound_to_bot_owner_chat_topic_and_message(self):
        panel = await self.open()
        changes = [{'message_thread_id': 84}, {'message_id': 999},
                   {'chat': {'id': 456, 'type': 'private'}}, {'from': {'id': 555}}, {'date': 0}]
        for change in changes:
            before = len(self.deliveries())
            await self.click(panel, '下一页', **change)
            self.assertEqual(len(self.deliveries()), before)
        actor = self.click_update(panel, '下一页')
        actor['callback_query']['from']['id'] = 456
        self.ui.ingest(actor)
        await self.ui.handle_one()
        self.assertEqual(len(self.deliveries()), 1)

    async def test_expired_and_old_revision_tokens_explain_without_edit(self):
        first = await self.open()
        current = await self.click(first, '下一页')
        count = len(self.deliveries())
        await self.click(first, '下一页')
        self.assertEqual(len(self.deliveries()), count)
        self.assertIn('过期或失效', self.calls[-1][1]['text'])
        self.clock += self.ui.TTL + 1
        await self.click(current, '刷新')
        self.assertEqual(len(self.deliveries()), count)

    async def test_close_edits_and_invalidates_callbacks(self):
        first = await self.open()
        closed = await self.click(first, '关闭')
        self.assertEqual(closed['text'], '已关闭代理列表。')
        self.assertEqual(self.state('ag_panels')['state'], 'closed')
        count = len(self.deliveries())
        await self.click(first, '刷新')
        self.assertEqual(len(self.deliveries()), count)

    async def test_queued_rebinding_rejects_without_reading_original_session(self):
        update = self.update()
        self.ui.ingest(update)
        self.router.targets['a']['thread_id'] = 'replacement'
        await self.ui.handle_one()
        self.assertEqual(self.catalog_calls, [])
        self.assertIn('绑定已变化', self.panel()['text'])
        self.assertEqual(self.panel()['message_thread_id'], 42)

    async def test_scope_and_binding_rechecked_after_catalog_await_before_send(self):
        async def move(root):
            self.denied.add(root)
        self.catalog_hook = move
        panel = await self.open()
        self.assertNotIn('public preview', panel['text'])
        self.assertIn('绑定已变化', panel['text'])
        self.assertEqual(self.state()['state'], 'failed')

    async def test_existing_panel_invalid_after_rebinding_or_scope_change(self):
        first = await self.open()
        self.denied.add('native-a')
        before = len(self.catalog_calls)
        await self.click(first, '刷新')
        self.assertEqual(len(self.catalog_calls), before)
        self.assertEqual(len(self.deliveries()), 1)

    async def test_unknown_send_result_is_preserved_without_retry_or_second_notice(self):
        self.ack = None
        update = self.update()
        self.ui.ingest(update)
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertEqual(self.state('ag_panels')['state'], 'uncertain')
        self.assertFalse(await self.ui.handle_one())
        self.ui.recover()
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.deliveries()), 1)

    async def test_notice_unknown_ack_is_also_preserved_without_retry(self):
        self.ack = None
        self.ui.ingest(self.update(topic=None))
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.deliveries()), 1)

    async def test_wrong_message_chat_or_topic_ack_is_unknown_without_followup(self):
        for acknowledgement in ({'message_id': 900, 'chat': {'id': 456}},
                                {'message_id': 901, 'message_thread_id': 84},
                                {'message_id': True}, {'message_id': 0}):
            self.ack = acknowledgement
            self.ui.ingest(self.update())
            await self.ui.handle_one()
            self.assertEqual(self.state()['state'], 'uncertain')
            self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.deliveries()), 4)

    async def test_active_panel_and_random_callbacks_survive_restart(self):
        panel = await self.open()
        catalog = self.ui.catalog
        self.ui = AgentsMenu(self.router, clock=lambda: self.clock)
        self.ui.catalog = catalog
        self.ui.recover()
        refreshed = await self.click(panel, '下一页')
        self.assertEqual(refreshed['message_id'], panel['message_id'])
        self.assertIn('第 2/3 页', refreshed['text'])

    async def test_transport_failure_and_server_error_are_never_blindly_retried(self):
        for failure in (ConnectionError('lost ack'), TelegramError('sendMessage', 500, 'server')):
            self.failures['sendMessage'] = [failure]
            self.ui.ingest(self.update())
            await self.ui.handle_one()
            self.assertEqual(self.state()['state'], 'uncertain')
            self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.deliveries()), 2)

    async def test_send_429_retries_after_deadline_using_same_panel(self):
        self.failures['sendMessage'] = [TelegramError('sendMessage', 429, 'rate limit', retry_after=7)]
        self.ui.ingest(self.update())
        await self.ui.handle_one()
        panel_id = self.state()['panel_id']
        self.assertEqual(self.state()['state'], 'pending')
        self.assertFalse(await self.ui.handle_one())
        self.clock += 7
        self.assertTrue(await self.ui.handle_one())
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(self.state()['panel_id'], panel_id)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ag_panels').fetchone()[0], 1)

    async def test_edit_429_keeps_old_panel_until_safe_retry(self):
        panel = await self.open()
        self.failures['editMessageText'] = [TelegramError('editMessageText', 429, 'rate limit', retry_after=3)]
        await self.click(panel, '下一页')
        self.assertEqual(self.state()['state'], 'pending')
        self.assertEqual(self.state('ag_panels')['state'], 'active')
        self.clock += 3
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'done')
        self.assertIn('第 2/3 页', self.panel()['text'])
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.calls), 1)

    async def test_message_not_modified_is_success(self):
        panel = await self.open()
        self.failures['editMessageText'] = [TelegramError('editMessageText', 400, 'message is not modified')]
        await self.click(panel, '刷新')
        self.assertEqual(self.state()['state'], 'done')
        self.assertEqual(self.state('ag_panels')['state'], 'active')

    async def test_restart_keeps_processing_and_creating_uncertain(self):
        self.failures['sendMessage'] = [TelegramError('sendMessage', 429, 'rate limit', retry_after=1)]
        self.ui.ingest(self.update())
        await self.ui.handle_one()
        self.ui.recover()
        self.assertEqual(self.state('ag_panels')['state'], 'uncertain')
        self.clock += 2
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertEqual(len(self.deliveries()), 1)
        self.ui.ingest(self.update(topic=84))
        self.db.execute("UPDATE ag_jobs SET state='processing' WHERE state='pending'")
        self.ui.recover()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertFalse(await self.ui.handle_one())

    async def test_timeout_during_delivery_is_unknown_and_never_followed_by_notice(self):
        self.ui.TIMEOUT = .02
        async def delayed():
            await asyncio.Event().wait()
        self.send_hook = delayed
        self.ui.ingest(self.update())
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertEqual(len(self.deliveries()), 1)

    async def test_scope_change_after_send_preserves_uncertain_delivery(self):
        async def move():
            self.denied.add('native-a')
        self.send_hook = move
        self.ui.ingest(self.update())
        await self.ui.handle_one()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertEqual(self.state('ag_panels')['state'], 'uncertain')
        self.assertEqual(len(self.deliveries()), 1)

    async def test_unicode_detail_stays_under_telegram_limit_without_interpreting_paths(self):
        panel = await self.open()
        base = self.ui.catalog.detail
        async def large(root, agent):
            result = await base(root, agent)
            result['task'] = '🧪' * 5000
            result['latest_reply'] = '/tmp/report.pdf\n' + '结果🧪' * 5000
            return result
        self.ui.catalog.detail = large
        detail = await self.click(panel, 'agent_0')
        self.assertLessEqual(utf16_size(detail['text']), 3800)
        self.assertIn('已截断', detail['text'])
        self.assertIn('/tmp/report.pdf', detail['text'])
        self.assertNotIn('parse_mode', detail)

    async def test_independent_topics_run_concurrently(self):
        gate, started, second_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def wait_for_a(root):
            if root == 'native-a':
                started.set()
                await gate.wait()
            else:
                second_done.set()
        self.catalog_hook = wait_for_a
        self.ui.ingest(self.update(topic=42))
        self.ui.ingest(self.update(topic=84))
        worker = asyncio.create_task(self.ui.loop())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(second_done.wait(), 1)
            await asyncio.sleep(.02)
            self.assertTrue(any(m.get('message_thread_id') == 84 for m in self.messages.values()))
            self.assertFalse(any(m.get('message_thread_id') == 42 for m in self.messages.values()))
            gate.set()
            await asyncio.sleep(.15)
            self.assertTrue(any(m.get('message_thread_id') == 42 for m in self.messages.values()))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
