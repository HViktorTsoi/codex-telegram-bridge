"""Persistent status must survive without any live-draft refresh or resend."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from bridge import TelegramError
from completion_delivery import CompletionDelivery
from message_format import utf16_size
from test_completion_delivery import ApiError
from topics_bridge import TargetBridge


class PersistentStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.now = 1000
        self.bridges = {}
        self.calls, self.messages, self.failures = [], {}, {}
        self.sequence, self.after_call = 100, None
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                failures = test.failures.get(method, [])
                if failures:
                    raise failures.pop(0)
                if method in ('sendMessage', 'sendRichMessage'):
                    test.sequence += 1
                    result = {'message_id': test.sequence,
                              'chat': {'id': int(params['chat_id'])},
                              'message_thread_id': params.get('message_thread_id')}
                    if 'text' in params:
                        result['text'] = params['text']
                    if 'rich_message' in params:
                        result['rich_message'] = copy.deepcopy(params['rich_message'])
                    if 'reply_markup' in params:
                        result['reply_markup'] = copy.deepcopy(params['reply_markup'])
                    test.messages[test.sequence] = result
                elif method in ('editMessageText', 'editMessageReplyMarkup'):
                    result = test.messages[params['message_id']]
                    if method == 'editMessageText':
                        result.pop('text', None)
                        result.pop('rich_message', None)
                        if 'text' in params:
                            result['text'] = params['text']
                        if 'rich_message' in params:
                            result['rich_message'] = copy.deepcopy(params['rich_message'])
                        # Treat an omitted keyboard conservatively: a text edit
                        # must explicitly preserve this visible status control.
                        result.pop('reply_markup', None)
                    if 'reply_markup' in params:
                        result['reply_markup'] = copy.deepcopy(params['reply_markup'])
                    result = copy.deepcopy(result)
                elif method == 'deleteMessage':
                    test.messages.pop(params['message_id'], None)
                    result = True
                elif method in ('sendMessageDraft', 'sendRichMessageDraft'):
                    result = True  # Only the explicit legacy-mode test allows it.
                else:
                    raise AssertionError('Unexpected display API: ' + method)
                if test.after_call:
                    await test.after_call(method, params)
                return copy.deepcopy(result)

        self.telegram = Telegram()
        for name, topic in (('a', 42), ('b', 84)):
            directory = self.path / name
            directory.mkdir()
            config = {'owner_id': '123', 'thread_id': 'native-' + name, 'socket_path': '/unused',
                      'telegram_state_dir': str(self.path), 'notification_mode': 'completion',
                      'preview_style': 'status-bar', 'native_mirror': False}
            target = {'key': name, 'name': name, 'thread_id': 'native-' + name, 'message_thread_id': topic}
            bridge = TargetBridge(config, directory, target, lambda: '')
            bridge.telegram = self.telegram
            bridge.ready.set()
            self.bridges[name] = bridge
            self.restart(name)

    async def asyncTearDown(self):
        for bridge in self.bridges.values():
            bridge.store.db.close()
        self.tmp.cleanup()

    def restart(self, name='a'):
        bridge = self.bridges[name]
        bridge.delivery = CompletionDelivery(bridge, clock=lambda: self.now)
        return bridge.delivery

    def delivery(self, name='a'):
        return self.bridges[name].delivery

    def turn(self, tid='a-work', status='inProgress', started=None):
        return {'id': tid, 'status': status, 'startedAt': self.now - 1 if started is None else started}

    def accept(self, uid=7, name='a'):
        bridge = self.bridges[name]
        bridge.store.accept({'update_id': uid}, {'message_id': uid, 'chat': {'id': 123, 'type': 'private'},
                                               'from': {'id': 123}, 'text': '执行任务',
                                               'message_thread_id': bridge.target['message_thread_id']})
        with bridge.store.db:
            bridge.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?', (int(self.now * 1000), uid))

    def pending_identity(self, uid=7, name='a'):
        bridge = self.bridges[name]
        return {'native_id': bridge.thread_id, 'chat_id': bridge.owner,
                'topic_id': bridge.target['message_thread_id'], 'turn_id': None, 'source_id': uid}

    def identity(self, tid='a-work', name='a'):
        return dict(self.delivery(name)._group(tid))

    def output(self, text='第一段工作文本', tid='a-work', iid='first', name='a'):
        bridge = self.bridges[name]
        bridge.store.enqueue_at('turn:' + tid + ':' + iid, bridge.owner, text, bridge.target['message_thread_id'])

    async def update(self, tid='a-work', name='a', status='inProgress', started=None, **extra):
        await self.delivery(name).update(self.telegram, turns=[self.turn(tid, status, started)], **extra)

    def calls_for(self, *methods):
        return [(method, params) for method, params in self.calls if method in methods]

    def sends(self):
        return self.calls_for('sendMessage', 'sendRichMessage')

    @staticmethod
    def body_text(message):
        return message.get('text') or message.get('rich_message', {}).get('markdown', '')

    def keyboard_text(self, message):
        keyboard = message['reply_markup']['inline_keyboard']
        return '\n'.join(button['text'] for row in keyboard for button in row)

    def assert_status_keyboard(self, message, text=None):
        rows = message['reply_markup']['inline_keyboard']
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 1)
        self.assertIn(len(rows[1]), (1, 2))
        for row in rows:
            self.assertEqual(row[0]['disabled'], {})
            self.assertNotIn('callback_data', row[0])
        if len(rows[1]) == 2:
            control = rows[1][1]
            self.assertEqual(control['text'], '⏹ 停止当前任务')
            self.assertNotIn('disabled', control)
            self.assertRegex(control['callback_data'], r'^stop:[0-9a-f]{24}$')
        if text:
            self.assertIn(text, self.keyboard_text(message))

    def assert_no_drafts(self):
        self.assertEqual(self.calls_for('sendMessageDraft', 'sendRichMessageDraft'), [])

    async def drain(self, name='a'):
        bridge = self.bridges[name]
        rows = []
        for _ in range(100):
            row = bridge.delivery.next_row()
            if row is None:
                return rows
            rows.append((row['key'], row['text'], bridge.delivery.silent(row)))
            async with bridge.delivery.preview_lock:
                await bridge.send_one(row)
        self.fail('Canonical output did not drain')

    async def test_pending_receipt_is_persistent_before_native_task_exists(self):
        self.accept()
        await self.delivery().receipt(self.telegram)
        self.assertEqual(len(self.sends()), 1)
        params = self.sends()[0][1]
        self.assertIs(params['disable_notification'], True)
        self.assert_status_keyboard(params, '已收到')
        preview = self.delivery().body_preview.load(self.pending_identity())
        self.assertIn(preview['message_id'], self.messages)
        self.assertEqual(self.bridges['a'].store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.assert_no_drafts()

    async def test_native_acceptance_adopts_placeholder_and_edits_same_message(self):
        self.accept()
        delivery, store = self.delivery(), self.bridges['a'].store
        await delivery.receipt(self.telegram)
        mid = delivery.body_preview.load(self.pending_identity())['message_id']
        store.mark(7, 'running', 'a-work')
        self.output('正文取代占位文本')
        self.now += 1
        await self.update()
        self.assertEqual(delivery.body_preview.load(self.identity())['message_id'], mid)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('正文取代占位文本', self.body_text(self.messages[mid]))
        self.assert_status_keyboard(self.messages[mid], '正在工作')
        self.assertTrue(self.calls_for('editMessageText'))
        self.assertTrue(all('reply_markup' in p for _,p in self.calls_for('editMessageText')))
        self.assert_no_drafts()

    async def test_cold_receipt_with_bound_turn_waits_persistently_before_history_connects(self):
        self.accept()
        delivery, store = self.delivery(), self.bridges['a'].store
        store.mark(7, 'running', 'a-work')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        await delivery.receipt(self.telegram, connected=False)
        self.assertEqual(len(self.sends()), 1)
        mid = delivery.body_preview.load(self.pending_identity())['message_id']
        self.assert_status_keyboard(self.messages[mid], '等待连接')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.output('连接恢复后的真实工作文本')
        self.now += 1
        await self.update()
        self.assertEqual(delivery.body_preview.load(self.identity())['message_id'], mid)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('连接恢复后的真实工作文本', self.body_text(self.messages[mid]))
        self.assert_status_keyboard(self.messages[mid], '正在工作')
        self.assert_no_drafts()

    async def test_new_pending_input_moves_placeholder_once_then_adopts_it(self):
        self.accept(7)
        await self.delivery().receipt(self.telegram)
        first_mid = self.delivery().body_preview.load(self.pending_identity(7))['message_id']
        self.now += 1
        self.accept(8)
        await self.delivery().receipt(self.telegram)
        current_mid = self.delivery().body_preview.load(self.pending_identity(8))['message_id']
        self.assertGreater(current_mid, first_mid)
        self.assertNotIn(first_mid, self.messages)
        self.assertEqual(len(self.sends()), 2)
        self.bridges['a'].store.mark(7, 'running', 'a-work')
        self.bridges['a'].store.mark(8, 'running', 'a-work')
        self.output()
        self.now += 1
        await self.update()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.delivery().body_preview.load(self.identity())['message_id'], current_mid)
        self.assertIn('第一段工作文本', self.body_text(next(iter(self.messages.values()))))
        self.assert_status_keyboard(next(iter(self.messages.values())), '正在工作')
        self.assert_no_drafts()

    async def test_unassigned_newer_receipt_cannot_be_stolen_by_earlier_native_task(self):
        delivery, store = self.delivery(), self.bridges['a'].store
        self.accept(7)
        await delivery.receipt(self.telegram)
        mid = delivery.body_preview.load(self.pending_identity(7))['message_id']
        self.now += 1
        self.accept(8)
        await delivery.receipt(self.telegram)
        old_mid = mid
        mid = delivery.body_preview.load(self.pending_identity(8))['message_id']
        self.assertGreater(mid, old_mid)
        self.assertNotIn(old_mid, self.messages)
        store.mark(7, 'running', 'a-work')
        self.output('任务 A 的正式结果')
        self.now += 1
        await self.update(started=1000)
        await delivery.receipt(self.telegram)
        self.assertEqual(len(self.sends()), 2)
        self.assertFalse(delivery.body_preview.load(self.identity()).get('message_id'))
        self.assertEqual(delivery.body_preview.load(self.pending_identity(8))['message_id'], mid)
        delivery.seal(self.turn(status='completed', started=1000))
        final_a = delivery.next_row()
        self.assertTrue(final_a['key'].startswith('completion-notice:'))
        store.mark(8, 'running', 'a-next')
        self.output('任务 B 的工作文本', tid='a-next')
        self.now += 1
        await self.update(tid='a-next', started=1002)
        self.assertEqual(delivery.body_preview.load(self.identity('a-next'))['message_id'], mid)
        self.assertEqual(len(self.sends()), 2)
        self.assertIn('任务 B 的工作文本', self.body_text(self.messages[mid]))
        b_markup = copy.deepcopy(self.messages[mid]['reply_markup'])
        async with delivery.preview_lock:
            await self.bridges['a'].send_one(final_a)
        self.assertIn(mid, self.messages)
        self.assertEqual(self.messages[mid]['reply_markup'], b_markup)
        self.assert_status_keyboard(self.messages[mid], '正在工作')
        self.assertEqual([p['message_id'] for _, p in self.calls_for('deleteMessage')], [old_mid])
        self.assertEqual(sum(p.get('disable_notification') is not True for _, p in self.sends()), 1)
        self.assert_no_drafts()

    async def test_status_survives_refresh_gap_and_restart_without_new_message(self):
        self.output()
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        cached = copy.deepcopy(self.messages[mid]['reply_markup'])
        self.now += 90  # A live draft would have expired long before this.
        self.restart()
        self.assertEqual(self.messages[mid]['reply_markup'], cached)
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.calls_for('editMessageText'), [])
        self.assertEqual(self.calls_for('editMessageReplyMarkup')[-1][1]['message_id'], mid)
        self.assert_status_keyboard(self.messages[mid], '正在工作')
        self.assert_no_drafts()

    async def test_body_edits_preserve_markup_and_heartbeat_changes_only_markup(self):
        self.output('第一段')
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.output('第二段', iid='second')
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.calls_for('editMessageText')), 1)
        self.assertIn('第一段\n\n────────────\n\n第二段', self.body_text(self.messages[mid]))
        self.assert_status_keyboard(self.messages[mid])
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method,_ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assert_no_drafts()

    async def test_switching_current_draft_body_to_bar_reuses_existing_message(self):
        bridge = self.bridges['a']
        bridge.config['preview_style'] = 'status-draft'
        self.restart()
        self.output()
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.assertNotIn('reply_markup', self.messages[mid])
        bridge.config['preview_style'] = 'status-bar'
        self.restart()
        self.now += 15
        before = len(self.calls)
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.delivery().body_preview.load(self.identity())['message_id'], mid)
        self.assert_status_keyboard(self.messages[mid], '正在工作')
        self.assertFalse(any(method.endswith('Draft') for method,_ in self.calls[before:]))

    async def test_unknown_pending_create_ack_survives_adoption_and_restart_without_resend(self):
        self.accept()
        self.failures['sendRichMessage'] = [ConnectionError('Lost create acknowledgement')]
        await self.delivery().receipt(self.telegram)
        self.assertEqual(len(self.sends()), 1)
        self.bridges['a'].store.mark(7, 'running', 'a-work')
        self.output()
        self.now += 15
        self.restart()
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.delivery().body_preview.load(self.identity()).get('delivery'), 'uncertain')
        self.assert_no_drafts()

    async def test_create_rate_limit_keeps_intent_when_native_task_adopts_it(self):
        self.accept()
        self.failures['sendRichMessage'] = [ApiError(429, retry_after=30)]
        await self.delivery().receipt(self.telegram)
        self.bridges['a'].store.mark(7, 'running', 'a-work')
        self.output()
        self.now += 15
        self.restart()
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(len(self.messages), 1)
        self.assert_status_keyboard(next(iter(self.messages.values())), '正在工作')
        self.assert_no_drafts()

    async def test_markup_rate_limit_retries_known_message_without_creating_another(self):
        self.output()
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.failures['editMessageReplyMarkup'] = [ApiError(429, retry_after=30)]
        self.now += 15
        await self.update()
        self.assertIn(mid, self.messages)
        cached = copy.deepcopy(self.messages[mid]['reply_markup'])
        self.restart()
        self.now += 15
        await self.update()
        self.assertEqual(self.messages[mid]['reply_markup'], cached)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual([p['message_id'] for _,p in self.calls_for('editMessageReplyMarkup')], [mid, mid])

    async def test_known_rich_rejection_falls_back_once_and_keeps_keyboard(self):
        self.failures['sendRichMessage'] = [TelegramError('sendRichMessage', 400, "can't parse rich message")]
        self.output()
        await self.update()
        self.assertEqual([m for m,_ in self.sends()], ['sendRichMessage', 'sendMessage'])
        self.assertEqual(len(self.messages), 1)
        self.assert_status_keyboard(next(iter(self.messages.values())), '正在工作')
        self.assert_no_drafts()

    async def test_pre_native_failure_remains_visible_and_next_receipt_is_fresh(self):
        self.accept()
        await self.delivery().receipt(self.telegram)
        first_mid = self.delivery().body_preview.load(self.pending_identity())['message_id']
        self.bridges['a'].store.mark(7, 'failed')
        self.now += 1
        await self.delivery().receipt(self.telegram)
        self.assert_status_keyboard(self.messages[first_mid], '未能提交')
        self.assertEqual(self.bridges['a'].store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.now += 1
        self.accept(8)
        await self.delivery().receipt(self.telegram)
        next_mid = self.delivery().body_preview.load(self.pending_identity(8))['message_id']
        self.assertNotEqual(first_mid, next_mid)
        self.assert_status_keyboard(self.messages[next_mid], '已收到')
        self.assert_no_drafts()

    async def test_final_reply_keeps_terminal_keyboard_before_temporary_preview_is_deleted(self):
        self.output('正式结果内容')
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(not silent for _,_,silent in rows), 1)
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        self.assert_status_keyboard(sends[-1][1], '已完成')
        self.assertNotIn(mid, self.messages)
        self.assertEqual(len(self.messages), 1)
        final = next(iter(self.messages.values()))
        self.assertIn('正式结果内容', self.body_text(final))
        self.assert_status_keyboard(final, '已完成')
        final_index = next(i for i,(method,p) in enumerate(self.calls)
                           if method in ('sendMessage','sendRichMessage') and p.get('disable_notification') is not True)
        delete_index = next(i for i,(method,_) in enumerate(self.calls) if method == 'deleteMessage')
        self.assertLess(final_index, delete_index)
        self.now += 90
        self.restart()
        await self.delivery().receipt(self.telegram)
        self.assertEqual(len(self.sends()), 2)
        self.assert_status_keyboard(final, '已完成')
        self.assert_no_drafts()

    async def test_failed_and_interrupted_final_statuses_keep_single_normal_notification(self):
        for tid,status,label in (('a-fail','failed','失败'),('a-stop','interrupted','停止')):
            with self.subTest(status=status):
                self.now += 15
                self.output('已保存工作文本', tid=tid)
                await self.update(tid=tid)
                self.delivery().seal(self.turn(tid,status))
                start = len(self.calls)
                rows = await self.drain()
                self.assertEqual(sum(not silent for _,_,silent in rows), 1)
                finals = [p for method,p in self.calls[start:] if method in ('sendMessage','sendRichMessage')]
                self.assertEqual(len(finals), 1)
                self.assert_status_keyboard(finals[0], label)
        self.assert_no_drafts()

    async def test_fast_final_ack_adopts_and_removes_source_only_placeholder(self):
        self.accept()
        bridge, delivery = self.bridges['a'], self.delivery()
        await delivery.receipt(self.telegram)
        mid = delivery.body_preview.load(self.pending_identity())['message_id']
        bridge.store.mark(7, 'running', 'a-work')
        delivery.watch(self.turn())
        self.output('在首次进度刷新前完成的结果')
        delivery.seal(self.turn(status='completed'))
        bridge.store.mark(7, 'done', 'a-work')
        # No update_status call has moved the source-keyed placeholder yet.
        self.assertFalse(delivery.body_preview.load(self.identity()))
        rows = await self.drain()
        self.assertEqual(sum(not silent for _, _, silent in rows), 1)
        self.assertEqual(len(self.sends()), 2)
        self.assertNotIn(mid, self.messages)
        self.assertEqual([p['message_id'] for _, p in self.calls_for('deleteMessage')], [mid])
        final = next(iter(self.messages.values()))
        self.assertIn('在首次进度刷新前完成的结果', self.body_text(final))
        self.assert_status_keyboard(final, '已完成')
        final_index = next(i for i, (method, params) in enumerate(self.calls)
                           if method in ('sendMessage', 'sendRichMessage')
                           and params.get('disable_notification') is not True)
        delete_index = next(i for i, (method, _) in enumerate(self.calls) if method == 'deleteMessage')
        self.assertLess(final_index, delete_index)
        self.assertIsNone(delivery.next_row())
        self.assert_no_drafts()

    async def test_fast_final_ack_crash_recovers_source_placeholder_with_style_off(self):
        self.accept()
        bridge, delivery = self.bridges['a'], self.delivery()
        await delivery.receipt(self.telegram)
        mid = delivery.body_preview.load(self.pending_identity())['message_id']
        bridge.store.mark(7, 'running', 'a-work')
        delivery.watch(self.turn())
        self.output('崩溃前已确认送达的快速结果')
        delivery.seal(self.turn(status='completed'))
        bridge.store.mark(7, 'done', 'a-work')
        self.assertFalse(delivery.body_preview.load(self.identity()))

        async def crash_after_canonical_ack(*args):
            return False

        delivery.complete_status = crash_after_canonical_ack
        row = delivery.next_row()
        async with delivery.preview_lock:
            await bridge.send_one(row)
        self.assertIn(mid, self.messages)
        self.assertEqual(self.calls_for('deleteMessage'), [])
        final_id = next(message_id for message_id in self.messages if message_id != mid)
        self.assert_status_keyboard(self.messages[final_id], '已完成')
        bridge.config['preview_style'] = 'plain'
        self.restart()
        self.now += 60
        await self.delivery().receipt(self.telegram)
        self.assertNotIn(mid, self.messages)
        self.assertIn(final_id, self.messages)
        self.assert_status_keyboard(self.messages[final_id], '已完成')
        self.assertEqual(len(self.sends()), 2)
        self.assertIsNone(self.delivery().next_row())
        await self.delivery().body_preview.cleanup(self.telegram)
        self.assertEqual([p['message_id'] for _, p in self.calls_for('deleteMessage')], [mid])
        self.assert_no_drafts()

    async def test_final_ack_crash_and_cleanup_retry_preserve_final_keyboard_without_resend(self):
        self.output()
        await self.update()
        delivery = self.delivery()
        mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed'))
        row = delivery.next_row()
        original = delivery.complete_status

        async def process_lost_before_finish(*args):
            return False

        delivery.complete_status = process_lost_before_finish
        async with delivery.preview_lock:
            await self.bridges['a'].send_one(row)
        delivery.complete_status = original
        self.assertIn(mid, self.messages)
        self.failures['deleteMessage'] = [ApiError(429, retry_after=30)]
        self.restart()
        await self.delivery().receipt(self.telegram)
        self.assertIn(mid, self.messages)
        self.now += 30
        self.restart()
        await self.delivery().receipt(self.telegram)
        self.assertNotIn(mid, self.messages)
        self.assertEqual(len(self.sends()), 2)
        self.assertIsNone(self.delivery().next_row())
        self.assert_status_keyboard(next(iter(self.messages.values())), '已完成')

    async def test_same_timestamp_new_native_task_keeps_its_bar_when_old_task_completes(self):
        self.output('A 的过程')
        await self.update(started=999)
        old_mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.delivery().seal(self.turn(status='completed',started=999))
        old_final = self.delivery().next_row()
        self.output('B 的过程',tid='a-next')
        self.now += 1
        await self.update(tid='a-next',started=999)
        mid = self.delivery().body_preview.load(self.identity('a-next'))['message_id']
        next_markup = copy.deepcopy(self.messages[mid]['reply_markup'])
        async with self.delivery().preview_lock:
            await self.bridges['a'].send_one(old_final)
        self.assertNotIn(old_mid,self.messages)
        self.assertEqual(self.messages[mid]['reply_markup'],next_markup)
        self.assert_status_keyboard(self.messages[mid],'正在工作')
        self.delivery().seal(self.turn('a-next','completed',999))
        await self.drain()
        finals = [p for _,p in self.sends() if p.get('disable_notification') is not True]
        self.assertEqual(len(finals),2)
        self.assertTrue(all('已完成' in self.keyboard_text(p) for p in finals))

    async def test_owner_revocation_or_repaired_topic_prevents_edits_and_cleanup(self):
        self.output()
        await self.update()
        bridge, delivery = self.bridges['a'], self.delivery()
        mid = delivery.body_preview.load(self.identity())['message_id']
        (self.path/'access.json').write_text(json.dumps({'dmPolicy':'allowlist','allowFrom':[]}))
        start = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls),start)
        (self.path/'access.json').write_text(json.dumps({'dmPolicy':'allowlist','allowFrom':['123']}))
        delivery.body_preview.finish(self.identity())
        bridge.target['message_thread_id'] = 420
        await delivery.body_preview.cleanup(self.telegram)
        self.assertIn(mid,self.messages)
        self.assertEqual(self.calls_for('deleteMessage'),[])

    async def test_slow_edit_in_one_topic_does_not_block_another_persistent_status(self):
        self.output()
        self.output('B 的过程',tid='b-work',name='b')
        await asyncio.gather(self.update(),self.update('b-work','b'))
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        entered,release = asyncio.Event(),asyncio.Event()

        async def block_a(method,params):
            if method=='editMessageText' and params['message_id']==mid:
                entered.set()
                await release.wait()

        self.after_call=block_a
        self.output('A 慢更新',iid='second')
        self.now +=15
        task=asyncio.create_task(self.update())
        try:
            await asyncio.wait_for(entered.wait(),0.5)
            await asyncio.wait_for(self.update('b-work','b'),0.5)
            self.assertFalse(task.done())
            b_mid=self.delivery('b').body_preview.load(self.identity('b-work','b'))['message_id']
            self.assert_status_keyboard(self.messages[b_mid],'正在工作')
        finally:
            release.set()
            await task
        self.assert_no_drafts()

    async def test_long_unicode_preview_is_bounded_but_formal_result_retains_every_character(self):
        body='原始开头\n'+'😀研究文本'*1600+'\n原始结尾'
        self.output(body)
        await self.update()
        mid=self.delivery().body_preview.load(self.identity())['message_id']
        self.assertLessEqual(utf16_size(self.body_text(self.messages[mid])),3800)
        self.assertIn('原始结尾',self.body_text(self.messages[mid]))
        self.delivery().seal(self.turn(status='completed'))
        rows=await self.drain()
        final=''.join(text for _,text,_ in rows)
        self.assertEqual(final.count('😀'),body.count('😀'))
        self.assertIn('原始开头',final)
        self.assertIn('原始结尾',final)
        self.assertEqual(sum(not silent for _,_,silent in rows),1)
        final_sends=[p for _,p in self.sends() if p.get('disable_notification') is not True]
        self.assertEqual(len(final_sends),1)
        self.assert_status_keyboard(final_sends[0],'已完成')

    async def test_legacy_plain_preview_keeps_using_draft_without_persistent_bar(self):
        bridge=self.bridges['a']
        bridge.config['preview_style']='plain'
        self.restart()
        self.output()
        await self.update()
        self.assertEqual(self.sends(),[])
        self.assertEqual(len(self.calls_for('sendMessageDraft')),1)

