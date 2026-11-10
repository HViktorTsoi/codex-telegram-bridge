import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from control_center import export_bundle, receive_bundle, questions_for, ident, write_json
from topics_bridge import TopicRouter


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.nodes = {}
        for host in ('A', 'B'):
            state = self.root / host
            state.mkdir()
            (state / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
            config = {'owner_id': '123', 'host_id': host, 'mode': 'topics', 'thread_id': host + '-native',
                      'socket_path': '/unused', 'telegram_state_dir': str(state), 'expected_bot_username': host + '_bot',
                      'root_target': 'manager', 'control_hosts': ['A', 'B'], 'targets': [
                          {'key': 'manager', 'name': 'Manager', 'thread_id': host + '-native', 'message_thread_id': 11,
                           'aliases': ['manager', host.lower()]},
                          {'key': 'paper', 'name': '论文', 'thread_id': host + '-paper', 'message_thread_id': 22,
                           'aliases': ['paper', '论文']}], 'control_hub': host == 'A'}
            if host == 'A':
                config['control_peers'] = [{'host_id': 'B', 'ssh_host': 'unused', 'code_dir': '/unused', 'state_dir': str(self.root / 'B')}]
            (state / 'config.json').write_text(json.dumps(config))
            router = TopicRouter(config, state)
            router.bot_id = 100 if host == 'A' else 200
            router.username = host + '_bot'
            test = self

            class Telegram:
                def __init__(self, host):
                    self.next_id, self.messages = 1000, {}
                    self.host = host

                async def call(self, method, **params):
                    test.calls.append((self.host, method, params))
                    if method == 'answerCallbackQuery':
                        return True
                    if method == 'sendMessage':
                        self.next_id += 1
                        msg = {**params, 'message_id': self.next_id, 'chat': {'id': 123, 'type': 'private'},
                               'from': {'id': 100 if self.host == 'A' else 200}, 'date': 1}
                        self.messages[msg['message_id']] = msg
                        return msg
                    if method == 'editMessageText':
                        msg = {**self.messages[params['message_id']], **params}
                        self.messages[msg['message_id']] = msg
                        return msg
                    raise AssertionError(method)

            router.telegram = Telegram(host)
            self.nodes[host] = router
            entries = []
            for target in config['targets']:
                entries.append({'host': host, 'thread_id': target['thread_id'], 'name': target['name'],
                                'target_key': target['key'], 'topic_id': target['message_thread_id'],
                                'aliases': target['aliases'], 'updated': time.time(), 'status': {'type': 'idle'},
                                'last_status': 'completed', 'latest_input': '请检查论文', 'latest_reply': '已完成检查。',
                                'questions': [], 'bot_username': host + '_bot'})
            write_json(state / 'control/local.json', {'host': host, 'generated': time.time(), 'entries': entries})
        self.sequence = 100
        for host, node in self.nodes.items():
            foreign = self.nodes['B' if host == 'A' else 'A']
            write_json(node.state_dir / 'control/global.json', {
                'nodes': [json.loads((foreign.state_dir / 'control/local.json').read_text())]})
        await self.sync()

    async def asyncTearDown(self):
        for node in self.nodes.values():
            for bridge in node.bridges.values():
                bridge.store.db.close()
            node.store.db.close()
        self.tmp.cleanup()

    async def sync(self):
        for node in self.nodes.values():
            await node.control.synchronize()

    def update(self, text, topic=11, sender=123, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'date': 1, 'from': {'id': sender},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, **extra}
        if topic:
            message['message_thread_id'] = topic
        return {'update_id': self.sequence, 'message': message}

    async def send(self, host, text, **extra):
        update = self.update(text, **extra)
        router = self.nodes[host]
        router.ingest(update)
        await router.control.handle_one()
        return update, router.telegram.messages.get(router.telegram.next_id)

    async def click(self, host, message, label, sender=123, topic=None):
        button = next(b for row in message['reply_markup']['inline_keyboard'] for b in row if label in b['text'])
        self.sequence += 1
        origin = dict(message)
        if topic is not None:
            origin['message_thread_id'] = topic
        callback = {'id': str(self.sequence), 'from': {'id': sender}, 'message': origin, 'data': button['callback_data']}
        router = self.nodes[host]
        router.ingest({'update_id': self.sequence, 'callback_query': callback})
        await router.control.handle_one()

    async def test_local_views_are_read_only_and_isolated_between_bots(self):
        for host in ('A', 'B'):
            _, panel = await self.send(host, '/tasks')
            self.assertIn('2 个已绑定', panel['text'])
            self.assertIn(host + ' /', panel['text']);self.assertNotIn(('B' if host == 'A' else 'A') + ' /', panel['text'])
            self.assertEqual(self.nodes[host].store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(self.nodes[host].store.db.execute('SELECT COUNT(*) FROM ui_jobs').fetchone()[0], 0)

    async def test_dispatch_target_chooser_uses_recent_user_access_order(self):
        node = self.nodes['A']
        node.recent.record('A-native', 'use-manager', 100)
        node.recent.record('A-paper', 'use-paper', 200)
        _, panel = await self.send('A', '/to', topic=None)
        labels = [b['text'] for row in panel['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn('论文', labels[0])
        self.assertIn('Manager', labels[1])
        self.assertEqual(node.recent.last('A-paper'), 200)

    async def test_alias_is_local_and_foreign_qualified_alias_is_rejected(self):
        await self.send('A', '@paper 检查')
        await self.send('A', '@B/paper 不应派单')
        rows = self.nodes['A'].store.db.execute('SELECT payload FROM cc_requests').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])['thread_id'], 'A-paper')
        self.assertIn('只支持本机', self.nodes['A'].store.db.execute('SELECT text FROM notices').fetchone()[0])
        await self.send('A', '@A/paper\n检查引用')
        request = json.loads(self.nodes['A'].store.db.execute('SELECT payload FROM cc_requests ORDER BY updated DESC LIMIT 1').fetchone()[0])
        self.assertEqual(request['text'], '检查引用')
        self.assertEqual((request['target_host'], request['thread_id']), ('A', 'A-paper'))

    async def test_empty_bound_session_has_generated_alias_and_dispatches_without_catalog(self):
        node = self.nodes['A']
        node.targets['paper']['name'] = 'Skyrim'
        node.targets['paper'].pop('aliases')
        write_json(node.state_dir / 'control/local.json',
                   {'host': 'A', 'generated': time.time(), 'entries': []})
        node.catalog_call = AsyncMock(side_effect=AssertionError('Aliases must not wait for native catalog'))

        _, panel = await self.send('A', '/aliases', topic=None)
        self.assertIn('@skyrim → Skyrim', panel['text'])
        entry = node.control.resolve('skyrim')
        self.assertEqual((entry['target_key'], entry['thread_id'], entry['topic_id']), ('paper', 'A-paper', 22))
        await self.send('A', '@skyrim 检查启动配置', topic=None)
        request = json.loads(node.store.db.execute('SELECT payload FROM cc_requests').fetchone()[0])
        self.assertEqual((request['target_host'], request['target_key'], request['thread_id'], request['topic_id']),
                         ('A', 'paper', 'A-paper', 22))
        self.assertEqual(request['text'], '检查启动配置')
        await self.sync()
        self.assertTrue(node.control.deliver_one())
        self.assertEqual(node.bridges['paper'].store.pending()['text'], '检查启动配置')
        self.assertFalse(node.control.deliver_one())
        node.catalog_call.assert_not_awaited()

    async def test_alias_uses_current_topic_after_repair_before_next_refresh(self):
        node = self.nodes['A']
        path = node.state_dir / 'control/local.json'
        local = json.loads(path.read_text())
        cached = next(e for e in local['entries'] if e['thread_id'] == 'A-paper')
        cached.update(target_key='obsolete-key', topic_id=8997, topic_missing=True)
        write_json(path, local)
        node.targets['paper'].update(message_thread_id=9004, topic_missing=False)

        entry = node.control.resolve('paper')
        self.assertEqual((entry['target_key'], entry['topic_id'], entry['topic_missing']), ('paper', 9004, False))
        await self.send('A', '@paper 修复后的派单', topic=None)
        request = json.loads(node.store.db.execute('SELECT payload FROM cc_requests').fetchone()[0])
        self.assertEqual((request['thread_id'], request['target_key'], request['topic_id']), ('A-paper', 'paper', 9004))

    async def test_unbound_cached_session_is_not_advertised_or_dispatchable(self):
        node = self.nodes['A']
        del node.targets['paper']
        entry = node.control.find({'host': 'A', 'thread_id': 'A-paper'})
        self.assertIsNotNone(entry)
        self.assertFalse(entry.get('target_key'))
        self.assertFalse(entry.get('topic_id'))
        with self.assertRaises(ValueError):
            node.control.resolve('paper')
        _, panel = await self.send('A', '/aliases', topic=None)
        self.assertNotIn('@paper', panel['text'])
        _, overview = await self.send('A', '/tasks', topic=None)
        self.assertIn('1 个已绑定', overview['text'])

    async def test_alias_overlay_rebuilds_configured_name_and_key_aliases(self):
        node = self.nodes['A']
        path = node.state_dir / 'control/local.json'
        local = json.loads(path.read_text())
        next(e for e in local['entries'] if e['thread_id'] == 'A-paper')['metadata_name'] = '论文'
        write_json(path, local)
        node.targets['paper'].update(name='Skyrim Notes', aliases=[' Dragon ', 'DRAGON', '', '  '])
        entry = node.control.resolve('dragon')
        self.assertEqual(entry['aliases'].count('dragon'), 1)
        self.assertNotIn('', entry['aliases'])
        for name in ('skyrim_notes', '论文', 'paper'):
            self.assertEqual(node.control.resolve(name)['thread_id'], 'A-paper')
        node.targets['paper']['aliases'] = []
        with self.assertRaises(ValueError):
            node.control.resolve('dragon')

    async def test_generated_alias_collision_requires_explicit_target_choice(self):
        node = self.nodes['A']
        for target in node.targets.values():
            target.update(name='Skyrim', aliases=[])
        write_json(node.state_dir / 'control/local.json',
                   {'host': 'A', 'generated': time.time(), 'entries': []})
        with self.assertRaisesRegex(ValueError, '重名'):
            node.control.resolve('skyrim')
        self.assertEqual(node.control.resolve('paper')['thread_id'], 'A-paper')

    async def test_foreign_cache_matching_bound_id_does_not_leak_metadata_or_aliases(self):
        node = self.nodes['A']
        write_json(node.state_dir / 'control/local.json', {'host': 'A', 'generated': time.time(), 'entries': [
            {'host': 'B', 'thread_id': 'A-paper', 'name': 'Foreign secret', 'cwd': '/other-machine',
             'target_key': 'paper', 'topic_id': 999, 'aliases': ['foreign'], 'status': {'type': 'idle'},
             'last_status': 'completed', 'latest_reply': 'FOREIGN PRIVATE REPLY',
             'questions': [{'id': 'private', 'kind': 'question', 'question': 'FOREIGN PRIVATE QUESTION'}]}]})
        entry = node.control.resolve('paper')
        self.assertEqual((entry['host'], entry['thread_id'], entry['topic_id']), ('A', 'A-paper', 22))
        self.assertNotIn('FOREIGN PRIVATE', json.dumps(entry))
        self.assertNotEqual(entry.get('cwd'), '/other-machine')
        self.assertEqual(entry['status'].get('type'), 'unknown')
        self.assertEqual(entry.get('questions', []), [])
        with self.assertRaises(ValueError):
            node.control.resolve('foreign')

    async def test_bound_session_without_metadata_is_shown_as_pending_not_idle(self):
        node = self.nodes['A']
        (node.state_dir / 'control/local.json').unlink()
        entry = node.control.resolve('paper')
        self.assertEqual(entry['status'].get('type'), 'unknown')
        self.assertFalse(entry.get('latest_input'))
        self.assertFalse(entry.get('latest_reply'))
        self.assertEqual(entry.get('questions', []), [])
        _, panel = await self.send('A', '/tasks', topic=None)
        self.assertIn('状态待同步', panel['text'])
        self.assertNotIn('空闲', panel['text'])
        self.assertNotIn('上轮完成', panel['text'])

    async def test_local_dispatch_and_reply_are_durable_and_deduplicated(self):
        await self.send('A', '@paper 检查引用')
        await self.sync();await self.sync()
        target = self.nodes['A'].bridges['paper']
        self.assertTrue(self.nodes['A'].control.deliver_one())
        self.assertFalse(self.nodes['A'].control.deliver_one())
        row = target.store.pending()
        self.assertEqual(row['text'], '检查引用')
        target.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'actual-existing-turn'}})})()
        await target.dispatch_one(row)
        target.codex.call.assert_awaited_once()
        method, params = target.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'A-paper'))
        self.assertEqual(set(params), {'threadId', 'input', 'clientUserMessageId'})
        target.store.enqueue_at('turn:actual-existing-turn:reply', '123', '**检查完毕**', 22)
        target.store.mark(row['id'], 'done', 'actual-existing-turn')
        await self.sync();await self.sync()
        source = self.nodes['A'].store.db
        relays = source.execute('SELECT * FROM notices WHERE label=?', ('A/论文',)).fetchall()
        self.assertEqual(len(relays), 1)
        self.assertEqual((relays[0]['topic_id'], relays[0]['rich_text']), (None, '**检查完毕**'))
        self.assertEqual(target.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
        self.assertEqual(source.execute('SELECT state FROM cc_requests').fetchone()[0], 'done')

    async def test_foreign_dispatch_does_not_reach_either_native_queue(self):
        await self.send('B', '/to A/paper 读批注')
        await self.sync()
        self.assertFalse(self.nodes['A'].control.deliver_one())
        self.assertFalse(self.nodes['B'].control.deliver_one())
        self.assertEqual(self.nodes['B'].store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)

    async def test_import_crash_recovery_preserves_original_inbox_and_output_boundary(self):
        await self.send('A', '@paper 测试')
        await self.sync();self.nodes['A'].control.deliver_one()
        db = self.nodes['A'].store.db
        saved = dict(db.execute('SELECT * FROM cc_deliveries').fetchone())
        with db:
            db.execute("UPDATE cc_deliveries SET state='importing'")
        self.nodes['A'].control.deliver_one()
        after = db.execute('SELECT * FROM cc_deliveries').fetchone()
        self.assertEqual((after['inbox_id'], after['after_rowid']), (saved['inbox_id'], saved['after_rowid']))
        self.assertEqual(self.nodes['A'].bridges['paper'].store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)

    async def test_changed_binding_never_redirects_accepted_work(self):
        await self.send('A', '@paper 测试')
        await self.sync()
        self.nodes['A'].targets['paper']['thread_id'] = 'another-native'
        self.nodes['A'].control.deliver_one()
        self.assertIsNone(self.nodes['A'].bridges['paper'].store.pending())
        await self.sync()
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT state FROM cc_requests').fetchone()[0], 'failed')

    async def test_owner_checks_and_callback_context_cannot_be_bypassed(self):
        await self.send('A', '@paper 偷发', sender=999)
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        _, panel = await self.send('A', '/tasks')
        await self.click('A', panel, '查看', sender=999)
        await self.click('A', panel, '查看', topic=999)
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        (self.nodes['A'].state_dir / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.send('A', '@paper 偷发')
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)

    async def test_explicit_prompt_reply_retains_destination(self):
        _, panel = await self.send('A', '/to')
        await self.click('A', panel, 'A/论文')
        prompt = self.nodes['A'].telegram.messages[self.nodes['A'].telegram.next_id]
        self.assertTrue(prompt['reply_markup']['force_reply'])
        self.assertNotIn('message_thread_id', prompt)
        await self.send('A', '只读批注', topic=None, reply_to_message={'message_id': prompt['message_id']})
        request = json.loads(self.nodes['A'].store.db.execute('SELECT payload FROM cc_requests').fetchone()[0])
        self.assertEqual((request['target_host'], request['thread_id'], request['text']), ('A', 'A-paper', '只读批注'))

    async def test_quoted_alias_dispatch_keeps_context_separate_from_task_and_destination(self):
        await self.send('A', '@paper 修改这里', reply_to_message={'message_id': 77, 'text': '引用原文'},
                        quote={'text': '原文', 'position': 2})
        await self.sync()
        self.assertTrue(self.nodes['A'].control.deliver_one())
        row = self.nodes['A'].bridges['paper'].store.pending()
        self.assertEqual((row['text'], row['message_thread_id']), ('修改这里', 22))
        self.assertEqual(json.loads(row['reply_json'])['reply_to_message_id'], 77)
        self.assertEqual(json.loads(row['reply_json'])['quote'], '原文')
        self.assertFalse(self.nodes['A'].control.deliver_one())

    async def test_management_commands_from_any_topic_open_main_chat_panels(self):
        for topic in (11, 999, None):
            for command in ('/tasks', '/tasks all', '/inbox', '/aliases', '/to'):
                _, panel = await self.send('A', command, topic=topic)
                self.assertNotIn('message_thread_id', panel)
                if command.startswith('/tasks'):
                    await self.click('A', panel, '刷新')
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertFalse(any(method == 'createForumTopic' for _, method, _ in self.calls))

    async def test_prompt_reply_in_wrong_topic_is_rejected_without_native_fallback(self):
        _, panel = await self.send('A', '/to', topic=22)
        await self.click('A', panel, 'A/论文')
        prompt = self.nodes['A'].telegram.messages[self.nodes['A'].telegram.next_id]
        await self.send('A', '不应执行', topic=22, reply_to_message={'message_id': prompt['message_id']})
        db = self.nodes['A'].store.db
        self.assertEqual(db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        notice = db.execute('SELECT topic_id,text FROM notices').fetchone()
        self.assertIsNone(notice['topic_id'])
        self.assertIn('派单回复已过期', notice['text'])

    async def test_legacy_topic_button_moves_next_panel_to_main_chat(self):
        _, panel = await self.send('A', '/tasks')
        panel['message_thread_id'] = 11
        self.nodes['A'].store.db.execute('UPDATE cc_buttons SET topic_id=11')
        self.nodes['A'].store.db.commit()
        await self.click('A', panel, '刷新')
        migrated = self.nodes['A'].telegram.messages[self.nodes['A'].telegram.next_id]
        self.assertNotEqual(migrated['message_id'], panel['message_id'])
        self.assertNotIn('message_thread_id', migrated)
        self.assertFalse(any(method == 'editMessageText' for _, method, _ in self.calls))
        await self.click('A', migrated, '刷新')
        self.assertEqual(self.calls[-1][1], 'editMessageText')

    async def test_attachments_do_not_fall_through_to_wrong_session(self):
        await self.send('A', '', photo=[{'file_id': 'a'}], caption='@paper 看图')
        self.assertEqual(self.nodes['A'].store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertIn('附件', self.nodes['A'].store.db.execute('SELECT text FROM notices').fetchone()[0])

    async def test_inbox_actions_remain_local(self):
        for host, node in self.nodes.items():
            path = node.state_dir / 'control/local.json'
            local = json.loads(path.read_text())
            local['entries'][0]['questions'] = [{'id': ident(host + ':q1'), 'kind': 'question', 'question': '请确认采用哪个方案？'}]
            write_json(path, local)
        _, panel = await self.send('A', '/inbox')
        self.assertIn('1 项', panel['text'])
        await self.click('A', panel, '查看')
        detail = self.nodes['A'].telegram.messages[panel['message_id']]
        await self.click('A', detail, '已处理')
        await self.sync()
        _, local = await self.send('A', '/inbox')
        _, other = await self.send('B', '/inbox')
        self.assertIn('0 项', local['text'])
        self.assertIn('1 项', other['text'])

    async def test_local_dispatch_does_not_need_hub_or_network(self):
        self.nodes['A'].config['control_hub'] = False
        await self.send('A', '@paper 本机执行')
        with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('No network allowed')):
            await self.sync()
        self.nodes['A'].control.deliver_one()
        self.assertEqual(self.nodes['A'].bridges['paper'].store.pending()['text'], '本机执行')

    async def test_old_foreign_cache_is_never_visible_even_with_legacy_peer_configuration(self):
        for command in ['/tasks all', '/inbox', '/aliases']:
            _, panel = await self.send('A', command)
            self.assertNotIn('B/', panel['text']);self.assertNotIn('B /', panel['text'])
        self.assertIsNone(self.nodes['A'].control.find({'host': 'B', 'thread_id': 'B-paper'}))
        with self.assertRaises(ValueError):
            await self.nodes['A'].control.dispatch(self.update('x')['message'], 900,
                self.nodes['B'].control.resolve('paper'), '不应派单')

    async def test_foreign_delivery_is_rejected_despite_legacy_allowlist(self):
        await self.send('A', '@paper 本机')
        request = json.loads(self.nodes['A'].store.db.execute('SELECT payload FROM cc_requests').fetchone()[0])
        request['source_host'] = 'B'
        result = receive_bundle(self.nodes['A'].state_dir, {'version': 1, 'owner': '123', 'incoming': [request]})
        self.assertIn(request['id'], result['rejected'])
        self.assertFalse(self.nodes['A'].control.deliver_one())

    async def test_legacy_cross_machine_queue_is_cancelled_without_interrupting_running_work(self):
        await self.send('A', '@paper 本机')
        request = json.loads(self.nodes['A'].store.db.execute('SELECT payload FROM cc_requests').fetchone()[0])
        db = self.nodes['A'].store.db;store = self.nodes['A'].bridges['paper'].store
        for index, state in [(1, 'pending'), (2, 'running')]:
            old = {**request, 'id': ident('legacy-' + str(index)), 'source_host': 'B'}
            update = self.update('旧跨机任务', topic=22);update['update_id'] = -index
            store.accept(update, update['message'])
            if state == 'running':store.mark(-index, 'running', 'already-running-native')
            with db:
                db.execute('INSERT INTO cc_deliveries(id,payload,state,inbox_id,updated) VALUES(?,?,?,?,?)',
                           (old['id'], json.dumps(old), 'queued', -index, time.time()))
        self.nodes['A'].control.isolate_local()
        self.assertEqual(store.db.execute('SELECT state FROM inbox WHERE id=-1').fetchone()[0], 'cancelled')
        self.assertEqual(store.db.execute('SELECT state FROM inbox WHERE id=-2').fetchone()[0], 'running')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM cc_deliveries WHERE state='cancelled'").fetchone()[0], 2)

    async def test_refresh_extracts_only_conversation_and_never_generates(self):
        node = self.nodes['A']
        (node.state_dir / 'control/local.json').write_text(json.dumps({'host': 'A', 'entries': [
            {'host': 'B', 'thread_id': 'unbound', 'cwd': '/other-machine', 'updated': 0}]}))
        calls = []
        async def catalog(method, params):
            calls.append(method)
            if method == 'thread/list':
                return {'data': [{'id': 'A-native', 'name': 'Manager', 'cwd': '/work/manager', 'updatedAt': time.time(), 'status': {'type': 'idle'}},
                                 {'id': 'unbound', 'name': '未绑定', 'updatedAt': time.time(), 'status': {'type': 'idle'}}]}
            return {'data': [{'id': 'turn1', 'status': 'completed', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': '继续'}]},
                {'type': 'reasoning', 'text': 'PRIVATE REASONING'},
                {'type': 'agentMessage', 'text': '请确认你希望采用哪种方案？'}]}]}
        node.catalog_call = catalog
        await node.control.refresh()
        local = json.loads((node.state_dir / 'control/local.json').read_text())
        self.assertEqual(len(local['entries']), 3)
        self.assertEqual(local['entries'][0]['cwd'], '/work/manager')
        self.assertIsNone(local['entries'][1]['cwd'])
        self.assertNotIn('PRIVATE REASONING', json.dumps(local))
        self.assertEqual(local['entries'][0]['questions'][0]['kind'], 'question')
        self.assertTrue(all(c in ('thread/list', 'thread/turns/list') for c in calls))

    async def test_empty_catalog_refresh_persists_all_bound_sessions_without_model_calls(self):
        node = self.nodes['A']
        write_json(node.state_dir / 'control/local.json',
                   {'host': 'A', 'generated': time.time(), 'entries': []})
        node.catalog_call = AsyncMock(return_value={'data': []})
        await node.control.refresh()
        local = json.loads((node.state_dir / 'control/local.json').read_text())
        self.assertEqual({e['thread_id'] for e in local['entries']}, {'A-native', 'A-paper'})
        for entry in local['entries']:
            self.assertEqual(entry['status'].get('type'), 'unknown')
            self.assertFalse(entry.get('latest_input'))
            self.assertFalse(entry.get('latest_reply'))
            self.assertEqual(entry.get('questions', []), [])
            self.assertEqual(node.control.resolve(entry['aliases'][0])['thread_id'], entry['thread_id'])
        node.catalog_call.assert_awaited_once()
        self.assertEqual(node.catalog_call.call_args.args[0], 'thread/list')

    async def test_first_native_metadata_replaces_placeholder_even_when_updated_is_zero(self):
        node = self.nodes['A']
        write_json(node.state_dir / 'control/local.json',
                   {'host': 'A', 'generated': time.time(), 'entries': []})
        node.catalog_call = AsyncMock(return_value={'data': []})
        await node.control.refresh()
        self.assertEqual(node.control.resolve('paper')['updated'], 0)
        self.assertTrue(node.control.resolve('paper').get('metadata_pending'))

        node.catalog_call = AsyncMock(side_effect=[
            {'data': [{'id': 'A-paper', 'name': 'Native Skyrim', 'cwd': '/work/skyrim',
                       'updatedAt': 0, 'status': {'type': 'idle'}}]},
            {'data': [{'id': 'first-turn', 'status': 'completed', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': '真实输入'}]},
                {'type': 'agentMessage', 'text': '真实回复'}]}]}])
        await node.control.refresh()
        entry = node.control.resolve('native_skyrim')
        self.assertFalse(entry.get('metadata_pending'))
        self.assertEqual(entry['status'], {'type': 'idle'})
        self.assertEqual((entry['name'], entry['metadata_name'], entry['cwd']),
                         ('论文', 'Native Skyrim', '/work/skyrim'))
        self.assertEqual((entry['last_status'], entry['latest_input'], entry['latest_reply']),
                         ('completed', '真实输入', '真实回复'))
        self.assertEqual(node.catalog_call.await_args_list[1].args[0], 'thread/turns/list')

    async def test_router_rename_removes_cached_native_name_alias_immediately(self):
        node = self.nodes['A']
        path = node.state_dir / 'control/local.json'
        local = json.loads(path.read_text())
        cached = next(e for e in local['entries'] if e['thread_id'] == 'A-paper')
        cached.update(name='Native Skyrim', metadata_name='Native Skyrim')
        write_json(path, local)
        self.assertEqual(node.control.resolve('native_skyrim')['target_key'], 'paper')

        target = node.targets['paper']
        binding = {field: target[field] for field in ('key', 'thread_id', 'message_thread_id')}
        node.rename_target('paper', binding, target['name'], 'Skyrim 新标题')
        self.assertEqual(node.control.resolve('skyrim_新标题')['thread_id'], 'A-paper')
        with self.assertRaises(ValueError):
            node.control.resolve('native_skyrim')
        # Explicit configured aliases remain valid after a name-only change.
        self.assertEqual(node.control.resolve('paper')['thread_id'], 'A-paper')

    async def test_duplicate_catalog_pages_cannot_make_bound_alias_ambiguous(self):
        node = self.nodes['A']
        calls = []

        async def catalog(method, params):
            calls.append((method, params))
            if method == 'thread/list':
                return {'data': [{'id': 'A-paper', 'name': 'Skyrim', 'updatedAt': 123,
                                  'status': {'type': 'idle'}}],
                        'nextCursor': None if params.get('cursor') else 'next-page'}
            return {'data': []}

        node.catalog_call = catalog
        await node.control.refresh()
        local = json.loads((node.state_dir / 'control/local.json').read_text())
        self.assertEqual(sum(e['thread_id'] == 'A-paper' for e in local['entries']), 1)
        self.assertEqual(node.control.resolve('skyrim')['thread_id'], 'A-paper')
        self.assertEqual(node.control.resolve('paper')['topic_id'], 22)
        self.assertEqual(len([c for c in calls if c[0] == 'thread/list']), 2)

    async def test_refresh_never_reuses_foreign_history_for_matching_native_id(self):
        node = self.nodes['A']
        write_json(node.state_dir / 'control/local.json', {'host': 'A', 'generated': time.time(), 'entries': [
            {'host': 'B', 'thread_id': 'A-paper', 'name': 'Foreign secret', 'updated': 123,
             'cwd': '/other-machine', 'aliases': ['foreign'], 'latest_reply': 'FOREIGN PRIVATE REPLY',
             'last_status': 'completed', 'questions': [{'id': 'private', 'kind': 'question',
                                                        'question': 'FOREIGN PRIVATE QUESTION'}]}]})
        node.catalog_call = AsyncMock(side_effect=[
            {'data': [{'id': 'A-paper', 'name': 'Skyrim', 'updatedAt': 123, 'status': {'type': 'idle'}}]},
            {'data': []}])
        await node.control.refresh()
        local = json.loads((node.state_dir / 'control/local.json').read_text())
        self.assertNotIn('FOREIGN PRIVATE', json.dumps(local))
        entry = node.control.resolve('skyrim')
        self.assertIsNone(entry.get('cwd'))
        self.assertEqual(entry.get('questions'), [])
        self.assertFalse(entry.get('latest_reply'))
        self.assertEqual(node.catalog_call.await_args_list[1].args[0], 'thread/turns/list')

    async def test_pending_order_uses_acceptance_time_for_negative_dispatch_ids(self):
        store = self.nodes['A'].bridges['paper'].store
        first = self.update('先到', topic=22);store.accept(first, first['message'])
        second = self.update('后到', topic=22);second['update_id'] = -100
        store.accept(second, second['message'])
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=received_ms+1 WHERE id=-100')
        self.assertEqual(store.pending()['text'], '先到')


class QuestionTests(unittest.TestCase):
    def entry(self, flags=None):
        return {'host': 'A', 'thread_id': 'native', 'status': {'type': 'active', 'activeFlags': flags or []}}

    def turn(self, text, tid='t'):
        return {'id': tid, 'status': 'completed', 'items': [{'type': 'agentMessage', 'text': text}]}

    def test_only_latest_turn_questions_and_no_ordinary_recommendations(self):
        self.assertEqual(questions_for(self.entry(), [self.turn('已提交成功。'), self.turn('请提供手机号？', 'old')]), [])
        self.assertEqual(questions_for(self.entry(), [self.turn('建议下一步看看视频。')]), [])
        self.assertEqual(questions_for(self.entry(), [self.turn('是否有误差是实验需要回答的问题？')]), [])

    def test_native_approval_is_not_presented_as_a_regular_answerable_question(self):
        result = questions_for(self.entry(['waitingOnApproval']), [self.turn('请确认方案？')])
        self.assertEqual(result[0]['kind'], 'native')
        self.assertIn('原会话', result[0]['question'])
