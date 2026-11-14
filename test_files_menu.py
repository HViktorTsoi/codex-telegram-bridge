"""Historical file browsing is local; deliberate resends keep their frozen route."""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from bridge import TelegramError
from attachments import media_items
from files_menu import FilesMenu
from topics_bridge import TopicRouter


class FilesMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {'owner_id': '123', 'telegram_state_dir': str(self.path), 'socket_path': '/unused',
            'thread_id': 'native-a', 'root_target': 'a', 'expected_bot_username': 'test_bot',
            'mode': 'topics', 'host_id': 'local', 'targets': [
                {'key': 'a', 'name': '会话 A', 'thread_id': 'native-a', 'message_thread_id': 42},
                {'key': 'b', 'name': '会话 B', 'thread_id': 'native-b', 'message_thread_id': 84}]}
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.ui = getattr(self.router, 'files_menu', None) or FilesMenu(self.router)
        self.clock, self.counter = time.time(), 100
        self.ui.clock = lambda: self.clock
        self.calls, self.messages, self.failures = [], {}, {}
        self.gate, self.send_started = None, asyncio.Event()
        test = self

        class Native:
            def __getattr__(self, method):
                raise AssertionError('Files must not invoke native operations: ' + method)

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if test.failures.get(method):
                    raise test.failures[method].pop(0)
                if method == 'answerCallbackQuery':
                    return True
                if method == 'editMessageText':
                    result = {**test.messages[params['message_id']], **copy.deepcopy(params)}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                if method == 'sendMessage' or method.startswith('send'):
                    if method != 'sendMessage':
                        test.send_started.set()
                        if test.gate:
                            await test.gate.wait()
                    test.counter += 1
                    result = {**copy.deepcopy(params), 'message_id': test.counter, 'date': 1,
                        'from': {'id': 444, 'is_bot': True}, 'chat': {'id': int(params['chat_id']), 'type': 'private'}}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                raise AssertionError(method)

            async def upload(self, method, field, path, **params):
                return await self.call(method, **params, **{field: str(path)})

        self.router.telegram = Telegram()
        for bridge in self.router.bridges.values():
            bridge.codex = Native()
            bridge.telegram = self.router.telegram

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def update(self, text='/files', topic=42, sender=123, **extra):
        self.counter += 1
        message = {'message_id': self.counter, 'date': 1, 'text': text,
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': sender}, **extra}
        if topic is not None:
            message['message_thread_id'] = topic
        return {'update_id': self.counter, 'message': message}

    def incoming(self, key='a', name='upload.pdf', file_id='file-one', unique_id=None, kind='document', stamp=None):
        target = self.router.targets[key]
        item = {'file_id': file_id, 'file_unique_id': unique_id or file_id, 'file_size': 64,
                'file_name': name, 'mime_type': 'application/pdf'}
        update = self.update('', topic=target['message_thread_id'], **{kind: item})
        if kind == 'photo':
            update['message']['photo'] = [{**item, 'width': 50, 'height': 50}]
        store = self.router.bridges[key].store
        store.accept(update, update['message'])
        if stamp is not None:
            with store.db:
                store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?', (int(stamp * 1000), update['update_id']))
        return update

    def outgoing(self, key='a', name='report.pdf', data=b'report bytes', serial=None, kind='document'):
        bridge = self.router.bridges[key]
        original = self.path / name
        original.write_bytes(data)
        media = (bridge.store.images.snapshot(original, '图表') if kind == 'image'
                 else bridge.store.images.snapshot_document(original, '报告'))
        self.counter += 1
        reference = serial or 'turn:report-' + str(self.counter) + ':media:0'
        media['delivery'] = 'sent'
        with bridge.store.db:
            bridge.store.db.execute('INSERT INTO outbox(key,chat_id,text,media_json,message_thread_id,message_id) VALUES(?,?,?,?,?,?)',
                (reference, '123', '', json.dumps(media), bridge.target['message_thread_id'], self.counter))
        return media

    def legacy_input_record(self, update, key='a'):
        """A previously persisted v44 input-button value, for upgrade regressions."""
        row = self.router.bridges[key].store.db.execute(
            'SELECT * FROM inbox WHERE id=?', (update['update_id'],)).fetchone()
        item = media_items(json.loads(row['message_json']))[0]
        return {'source': 'in', 'source_id': row['id'],
                'identity': json.dumps(['in', item['kind'], item['file_unique_id']],
                                       ensure_ascii=False, separators=(',', ':')),
                'name': item['file_name'], 'kind': item['kind'], 'size': item['file_size'],
                'mime': item.get('mime_type'), 'stamp': row['received_ms'] / 1000, 'item': item}

    async def open(self, key='a'):
        update = self.update(topic=self.router.targets[key]['message_thread_id'])
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return self.panel()

    def panel(self):
        candidates = [v for v in self.messages.values() if v.get('reply_markup', {}).get('inline_keyboard')]
        return copy.deepcopy(max(candidates, key=lambda v: v['message_id']))

    def click_update(self, panel, text, **origin_changes):
        buttons = [b for row in panel['reply_markup']['inline_keyboard'] for b in row]
        exact = [b for b in buttons if b['text'] == text or b['text'].split('. ', 1)[-1] == text]
        button = exact[0] if exact else next(b for b in buttons if text in b['text'])
        self.counter += 1
        return {'update_id': self.counter, 'callback_query': {'id': str(self.counter), 'from': {'id': 123},
            'data': button['callback_data'], 'message': {**copy.deepcopy(panel), **origin_changes}}}

    async def click(self, panel, text, **changes):
        update = self.click_update(panel, text, **changes)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return update

    def sends(self):
        return [(method, params) for method, params in self.calls if method.startswith('send') and method != 'sendMessage']

    def state(self):
        row = self.ui.db.execute('SELECT * FROM fl_sends ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    async def test_history_lists_only_codex_outputs_and_keeps_topics_separate(self):
        self.incoming(name='upload_A.pdf')
        self.outgoing(name='report_A.pdf')
        self.outgoing(key='b', name='private_B.pdf')
        panel = await self.open()
        self.assertIn('共 1 项', panel['text'])
        self.assertNotIn('upload_A.pdf', panel['text'])
        self.assertIn('report_A.pdf', panel['text'])
        self.assertNotIn('你上传的', panel['text'])
        self.assertIn('Codex 文件', panel['text'])
        self.assertNotIn('private_B.pdf', panel['text'])
        self.assertNotIn(str(self.path), panel['text'])
        self.assertEqual(self.sends(), [])
        self.assertNotIn('parse_mode', panel)
        self.assertTrue(all(len(b.get('callback_data', '')) < 64 for row in panel['reply_markup']['inline_keyboard'] for b in row))

    async def test_outgoing_photo_snapshot_keeps_matching_bot_and_topic_route(self):
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        media = self.outgoing(name='chart.png', data=image, kind='image')
        panel = await self.open()
        await self.click(panel, 'chart.png')
        self.assertEqual(self.sends()[0][0], 'sendPhoto')
        params = self.sends()[0][1]
        self.assertEqual(params['photo'], media['path'])
        self.assertEqual((params['chat_id'], params['message_thread_id']), ('123', 42))
        self.assertTrue(params['disable_notification'])
        self.assertEqual(self.state()['state'], 'sent')

    async def test_outgoing_uses_snapshot_after_original_deleted_and_does_not_grow_inventory(self):
        media = self.outgoing()
        (self.path / 'report.pdf').unlink()
        panel = await self.open()
        await self.click(panel, 'report.pdf')
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][1]['document'], media['path'])
        self.assertEqual(len(self.ui.inventory(self.router.bridges['a'])), 1)

    async def test_duplicate_updates_and_repeated_clicks_do_not_duplicate_upload(self):
        self.outgoing()
        panel = await self.open()
        update = await self.click(panel, 'report.pdf')
        self.ui.ingest(update)
        await self.ui.handle_one()
        await self.click(panel, 'report.pdf')
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.ui.db.execute('SELECT count(*) FROM fl_sends').fetchone()[0], 1)

    async def test_unknown_send_is_not_replayed_by_tick_or_recovery(self):
        self.outgoing()
        panel = await self.open()
        self.failures['sendDocument'] = [ConnectionError('lost acknowledgement')]
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.state()['state'], 'uncertain')
        self.ui.recover()
        await self.ui.handle_one()
        await self.ui._notices()
        self.assertEqual(len(self.sends()), 1)
        self.assertTrue(any('未自动重发' in p.get('text', '') for m, p in self.calls if m == 'sendMessage'))

    async def test_429_waits_for_retry_after_and_resends_only_the_same_request(self):
        self.outgoing()
        panel = await self.open()
        self.failures['sendDocument'] = [TelegramError('sendDocument', 429, 'slow down', retry_after=7)]
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.state()['state'], 'pending')
        self.assertFalse(await self.ui.send_one())
        self.clock += 8
        self.assertTrue(await self.ui.send_one())
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.sends()[0][1], self.sends()[1][1])
        self.assertEqual(self.state()['state'], 'sent')

    async def test_restart_during_send_turns_intent_uncertain_without_retry(self):
        self.outgoing()
        panel = await self.open()
        update = self.click_update(panel, 'report.pdf')
        self.ui.ingest(update)
        row = self.ui.db.execute('SELECT * FROM fl_jobs WHERE id=?', (update['update_id'],)).fetchone()
        await self.ui._process(row)
        with self.ui.db:
            self.ui.db.execute("UPDATE fl_sends SET state='sending'")
        self.ui.recover()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertFalse(await self.ui.send_one())
        self.assertEqual(self.sends(), [])

    async def test_dedup_preserves_output_versions_without_including_same_name_uploads(self):
        self.incoming(name='same.pdf', file_id='old', unique_id='same', stamp=self.clock - 20)
        self.incoming(name='same.pdf', file_id='new', unique_id='same', stamp=self.clock)
        first = self.outgoing(name='same.pdf', data=b'v1')
        second = self.outgoing(name='same.pdf', data=b'v2')
        self.outgoing(name='same.pdf', data=b'v1')
        files = self.ui.inventory(self.router.bridges['a'])
        self.assertEqual(len(files), 2)
        self.assertTrue(all(x['source'] == 'out' for x in files))
        self.assertEqual({x['media']['sha256'] for x in files}, {first['sha256'], second['sha256']})

    async def test_pagination_and_refresh_keep_last_page_and_expire_old_buttons(self):
        for i in range(14):
            self.outgoing(name=f'file-{i}.pdf', data=f'version {i}'.encode())
        panel = await self.open()
        self.assertIn('第 1/3 页', panel['text'])
        await self.click(panel, '下一页')
        second = self.panel()
        self.assertIn('第 2/3 页', second['text'])
        await self.click(second, '下一页')
        last = self.panel()
        self.assertIn('第 3/3 页', last['text'])
        await self.click(last, '刷新')
        self.assertIn('第 3/3 页', self.panel()['text'])
        await self.click(panel, 'file-13.pdf')
        self.assertEqual(self.sends(), [])

    async def test_other_bot_owner_topic_message_and_expired_panel_cannot_resend(self):
        self.outgoing()
        panel = await self.open()
        for changes in ({'from': {'id': 999}}, {'message_thread_id': 84}, {'message_id': 999}, {'date': 0}):
            await self.click(panel, 'report.pdf', **changes)
        update = self.click_update(panel, 'report.pdf')
        update['callback_query']['from']['id'] = 999
        self.ui.ingest(update)
        await self.ui.handle_one()
        self.clock += self.ui.TTL + 1
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.sends(), [])

    async def test_rebinding_and_bound_thread_mismatch_disable_old_entry_and_send(self):
        self.outgoing()
        panel = await self.open()
        bridge = self.router.bridges['a']
        entry = self.ui.entry_callback(bridge)
        self.assertTrue(entry.startswith('fl:open:'))
        bridge.store.set('bound_thread_id', 'another-native')
        self.assertIsNone(self.ui.entry_callback(bridge))
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.sends(), [])

    async def test_rebuilt_topic_can_browse_same_native_history_but_old_buttons_fail(self):
        self.outgoing()
        panel = await self.open()
        target, bridge = self.router.targets['a'], self.router.bridges['a']
        self.router.by_topic.pop(42)
        target['message_thread_id'] = 77
        bridge.target['message_thread_id'] = 77
        self.router.by_topic[77] = target
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.sends(), [])
        new = await self.open()
        self.assertIn('report.pdf', new['text'])
        await self.click(new, 'report.pdf')
        self.assertEqual(self.sends()[0][1]['message_thread_id'], 77)

    async def test_deleted_or_changed_snapshot_never_uploads(self):
        media = self.outgoing()
        panel = await self.open()
        Path(media['path']).write_bytes(b'changed!!!!!')
        await self.click(panel, 'report.pdf')
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.state()['state'], 'failed')
        self.failures.clear()
        Path(media['path']).unlink()
        new = await self.open()
        self.assertIn('快照已不可用', new['text'])
        disabled = next(b for row in new['reply_markup']['inline_keyboard'] for b in row if 'report.pdf' in b['text'])
        self.assertIn('disabled', disabled)

    async def test_symlink_or_foreign_target_snapshot_is_never_uploaded(self):
        media = self.outgoing(key='b', name='foreign.pdf')
        a = self.router.bridges['a']
        with a.store.db:
            a.store.db.execute('INSERT INTO outbox(key,chat_id,text,media_json,message_thread_id,message_id) VALUES(?,?,?,?,?,?)',
                ('bad-path', '123', '', json.dumps(media), 42, 900))
        panel = await self.open()
        disabled = next(b for row in panel['reply_markup']['inline_keyboard'] for b in row if 'foreign.pdf' in b['text'])
        self.assertIn('disabled', disabled)
        self.assertEqual(self.sends(), [])

    async def test_all_command_never_defaults_to_root_and_literal_foreign_commands_are_not_consumed(self):
        self.outgoing()
        update = self.update(topic=None)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        self.assertEqual(self.ui.db.execute('SELECT count(*) FROM fl_panels').fetchone()[0], 0)
        self.assertTrue(any('请在目标话题' in p.get('text', '') for m, p in self.calls))
        for update in (self.update('/files@other_bot'), self.update('/files argument'),
                       self.update('/files', document={'file_id': 'file'}),
                       self.update('/files', forward_origin={'type': 'user'})):
            self.assertFalse(self.ui.ingest(update))

    async def test_command_queue_freezes_binding_and_bot_identity_at_ingestion(self):
        self.outgoing()
        update = self.update()
        self.ui.ingest(update)
        self.router.targets['a']['thread_id'] = 'replacement'
        self.router.bridges['a'].thread_id = 'replacement'
        self.router.bridges['a'].target['thread_id'] = 'replacement'
        self.router.bridges['a'].store.set('bound_thread_id', 'replacement')
        await self.ui.handle_one()
        self.assertEqual(self.ui.db.execute('SELECT count(*) FROM fl_panels').fetchone()[0], 0)
        self.assertEqual(self.sends(), [])

    async def test_corrupt_legacy_timestamps_do_not_break_the_inventory(self):
        media = self.outgoing(serial='turn:legacy-time:media:0')
        os.utime(media['path'], (0, 0))
        bridge = self.router.bridges['a']
        with bridge.store.db:
            bridge.store.db.execute('INSERT INTO completion_turns '
                '(turn_id,chat_id,topic_id,native_id,started_at) VALUES(?,?,?,?,?)',
                ('legacy-time', '123', 42, bridge.thread_id, None))
            bridge.store.db.execute('INSERT INTO completion_rows VALUES(?,?,?,?)',
                ('turn:legacy-time:media:0', 'legacy-time', '123', 42))
        panel = await self.open()
        self.assertIn('report.pdf', panel['text'])
        self.assertIn('时间未知', panel['text'])
        with bridge.store.db:
            bridge.store.db.execute('UPDATE completion_turns SET started_at=? WHERE turn_id=?',
                                    ('invalid timestamp', 'legacy-time'))
        await self.click(panel, '刷新')
        self.assertIn('report.pdf', self.panel()['text'])
        self.assertIn('时间未知', self.panel()['text'])

    async def test_user_uploaded_media_are_not_listed_or_given_resend_buttons(self):
        kinds = ('document', 'photo', 'voice', 'audio', 'video', 'video_note', 'animation', 'sticker')
        for i, kind in enumerate(kinds):
            self.incoming(name=kind, file_id=kind, kind=kind, stamp=self.clock + i)
        panel = await self.open()
        self.assertEqual(self.ui.inventory(self.router.bridges['a']), [])
        self.assertIn('共 0 项', panel['text'])
        self.assertIn('还没有已回传的 Codex 文件', panel['text'])
        buttons = [b['text'] for row in panel['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons, ['刷新', '关闭'])
        self.assertEqual(self.router.bridges['a'].store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 8)
        self.assertEqual(self.sends(), [])

    async def test_preupgrade_input_resend_button_is_rejected(self):
        upload = self.incoming()
        self.outgoing()
        panel = await self.open()
        update = self.click_update(panel, 'report.pdf')
        token = update['callback_query']['data'][3:]
        with self.ui.db:
            self.ui.db.execute('UPDATE fl_buttons SET value=? WHERE token=?',
                               (json.dumps(self.legacy_input_record(upload)), token))
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        job = self.ui.db.execute('SELECT state,error FROM fl_jobs WHERE id=?', (update['update_id'],)).fetchone()
        self.assertEqual((job['state'], job['error']), ('failed', 'FilesError'))
        self.assertEqual(self.ui.db.execute('SELECT count(*) FROM fl_sends').fetchone()[0], 0)
        self.assertEqual(self.sends(), [])

    async def test_preupgrade_queued_input_resend_is_failed_without_network_upload(self):
        upload = self.incoming()
        self.outgoing()
        panel = await self.open()
        update = self.click_update(panel, 'report.pdf')
        self.ui.ingest(update)
        job = self.ui.db.execute('SELECT * FROM fl_jobs WHERE id=?', (update['update_id'],)).fetchone()
        await self.ui._process(job)
        self.assertEqual(self.state()['state'], 'pending')
        with self.ui.db:
            self.ui.db.execute('UPDATE fl_sends SET file_json=? WHERE id=?',
                (json.dumps(self.legacy_input_record(upload)), self.state()['id']))
        self.ui.recover()
        self.assertTrue(await self.ui.send_one())
        self.assertEqual(self.state()['state'], 'failed')
        self.assertFalse(await self.ui.send_one())
        self.assertEqual(self.sends(), [])

    async def test_slow_send_does_not_block_other_topic_menu_or_second_media_delivery(self):
        self.outgoing()
        panel = await self.open()
        self.gate = asyncio.Event()
        update = self.click_update(panel, 'report.pdf')
        self.ui.ingest(update)
        runner = asyncio.create_task(self.ui.loop())
        try:
            await asyncio.wait_for(self.send_started.wait(), 2)
            other = self.update(topic=84)
            self.ui.ingest(other)
            for _ in range(50):
                row = self.ui.db.execute('SELECT state FROM fl_jobs WHERE id=?', (other['update_id'],)).fetchone()
                if row and row[0] == 'done':
                    break
                await asyncio.sleep(.02)
            self.assertEqual(row[0], 'done')
            self.assertEqual(self.panel()['message_thread_id'], 84)
        finally:
            self.gate.set()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
