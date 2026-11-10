"""A single bottom status draft must remain independent of editable task text."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from completion_delivery import CompletionDelivery
from draft_position import StatusDraftPosition
from message_format import send_output, utf16_size
from test_completion_delivery import ApiError
from topics_bridge import RoutedStore


class StatusDraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.bridges, self.deliveries = {}, {}
        self.calls, self.messages, self.failures = [], {}, {}
        self.sequence, self.after_call = 100, None
        for name, topic in (('a', 42), ('b', 84)):
            store = RoutedStore(Path(self.tmp.name) / (name + '.sqlite'), 'native-' + name)
            bridge = SimpleNamespace(
                store=store, thread_id='native-' + name, owner='123', owner_allowed=lambda: True,
                target={'key': name, 'thread_id': 'native-' + name, 'message_thread_id': topic},
                config={'notification_mode': 'completion', 'preview_style': 'status-draft'})
            self.bridges[name] = bridge
            self.restart(name)
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                errors = test.failures.get(method, [])
                if errors:
                    raise errors.pop(0)
                if method in ('sendMessageDraft', 'sendRichMessageDraft'):
                    result = True
                elif method in ('sendMessage', 'sendRichMessage'):
                    test.sequence += 1
                    result = {'message_id': test.sequence,
                              'message_thread_id': params['message_thread_id']}
                    test.messages[test.sequence] = copy.deepcopy(result)
                elif method == 'editMessageText':
                    result = copy.deepcopy(test.messages[params['message_id']])
                elif method == 'deleteMessage':
                    test.messages.pop(params['message_id'], None)
                    result = True
                else:
                    raise AssertionError('Preview must not pin messages or call a model: ' + method)
                if test.after_call:
                    await test.after_call(method, params)
                return result

        self.telegram = Telegram()

    async def asyncTearDown(self):
        for bridge in self.bridges.values():
            bridge.store.db.close()
        self.tmp.cleanup()

    def restart(self, name='a'):
        self.deliveries[name] = CompletionDelivery(self.bridges[name], clock=lambda: self.now)

    def turn(self, tid='a-work', status='inProgress', started=1001):
        return {'id': tid, 'status': status, 'startedAt': started}

    def output(self, text='第一段正文', tid='a-work', iid='first', name='a'):
        bridge = self.bridges[name]
        bridge.store.enqueue_at('turn:' + tid + ':' + iid, '123', text,
                                bridge.target['message_thread_id'])

    async def update(self, name='a', tid=None, status='inProgress', **params):
        await self.deliveries[name].update(
            self.telegram, turns=[self.turn(tid or name + '-work', status)], **params)

    def calls_for(self, *methods):
        return [(method, params) for method, params in self.calls if method in methods]

    def drafts(self):
        return [params for _, params in self.calls_for('sendRichMessageDraft')]

    @staticmethod
    def body_text(params):
        if 'text' in params:
            return params['text']
        rich = params['rich_message']
        return rich.get('markdown') or rich.get('html') or str(rich.get('blocks'))

    def identity(self, tid='a-work', name='a'):
        return dict(self.deliveries[name]._group(tid))

    async def acknowledge(self, row, name='a'):
        delivery, store = self.deliveries[name], self.bridges[name].store
        delivery.begin(row)
        store.images.begin(row)
        sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'],
                                 rich_text=row['rich_text'], topic_id=row['message_thread_id'],
                                 media_json=row['media_json'], disable_notification=delivery.silent(row))
        store.images.sent(row, sent['message_id'])
        delivery.sent(row, sent['message_id'])
        if row['key'].startswith('completion-notice:'):
            await delivery.complete_status(self.telegram, row)

    async def drain(self, name='a'):
        delivery, rows = self.deliveries[name], []
        for _ in range(100):
            row = delivery.next_row()
            if row is None:
                return rows
            rows.append((row['key'], row['text'], delivery.silent(row)))
            await self.acknowledge(row, name)
        self.fail('Final output did not become idle')

    async def control_reply(self, name='a', key='input:8:reply'):
        delivery, store = self.deliveries[name], self.bridges[name].store
        store.enqueue_at(key, '123', '当前模型信息', self.bridges[name].target['message_thread_id'])
        row = delivery.next_row()
        self.assertTrue(row['key'].startswith(key))
        await self.acknowledge(row, name)
        delivery.reschedule_receipt()

    async def test_body_and_bottom_status_are_separate_and_body_precedes_single_draft(self):
        self.output('正文不应进入状态草稿')
        await self.update()
        sends = self.calls_for('sendRichMessage', 'sendMessage')
        self.assertEqual(len(sends), 1)
        self.assertIs(sends[0][1]['disable_notification'], True)
        self.assertIn('正文不应进入状态草稿', self.body_text(sends[0][1]))
        draft = self.drafts()[0]
        self.assertEqual(draft['message_thread_id'], 42)
        self.assertEqual(len(draft['rich_message']['blocks']), 1)
        status = draft['rich_message']['blocks'][0]['text']
        self.assertIn('正在工作', status)
        self.assertEqual(len(status.splitlines()), 2)
        self.assertNotIn('正文不应进入状态草稿', status)
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
        self.assertTrue(all(method != 'pinChatMessage' for method, _ in self.calls))
        self.assertIsNone(self.deliveries['a'].next_row())

    async def test_first_body_moves_receipt_status_once_and_persists_new_draft_identity(self):
        store, delivery = self.bridges['a'].store, self.deliveries['a']
        store.accept({'update_id': 7}, {'message_id': 7, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '开始这个任务'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1000000 WHERE id=7')
        await delivery.receipt(self.telegram)
        draft_id = self.drafts()[0]['draft_id']
        self.assertIn('已收到', self.drafts()[0]['rich_message']['blocks'][0]['text'])
        self.assertEqual(self.calls_for('sendRichMessage', 'sendMessage'), [])
        store.mark(7, 'running', 'a-work')
        self.output()
        self.now += 1
        await self.update()
        moved_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(moved_id, draft_id)
        self.assertEqual(self.identity()['draft_id'], moved_id)
        self.assertEqual(delivery._receipt_state()['draft_id'], moved_id)
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.now += 15
        await delivery.receipt(self.telegram)
        self.assertEqual(self.drafts()[-1]['draft_id'], moved_id)

    async def test_control_reply_redraw_restores_native_client_task_status_immediately(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        self.output('从电脑客户端发起的任务过程')
        await self.update()
        draft_id = self.drafts()[0]['draft_id']
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
        self.now += 1
        # A real /model or /status reply clears the client's only live draft.
        await self.control_reply()
        await delivery.receipt(self.telegram)
        self.assertEqual(len(self.drafts()), 2)
        self.assertNotEqual(self.drafts()[-1]['draft_id'], draft_id)
        self.assertIn('正在工作', self.drafts()[-1]['rich_message']['blocks'][0]['text'])
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
        self.assertEqual(self.calls_for('editMessageText'), [])

    async def test_pre_native_input_failure_replaces_receipt_with_one_warning_draft(self):
        store, delivery = self.bridges['a'].store, self.deliveries['a']
        store.accept({'update_id': 7}, {'message_id': 7, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '提交这条任务'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1000000 WHERE id=7')
        await delivery.receipt(self.telegram)
        draft_id = self.drafts()[0]['draft_id']
        store.mark(7, 'failed')
        self.now += 1
        await delivery.receipt(self.telegram)
        self.assertEqual(len(self.drafts()), 2)
        self.assertEqual(self.drafts()[-1]['draft_id'], draft_id)
        self.assertIn('本次消息未能提交', self.drafts()[-1]['rich_message']['blocks'][0]['text'])
        self.now += 15
        await delivery.receipt(self.telegram)
        self.assertEqual(len(self.drafts()), 2)
        self.assertEqual(self.calls_for('sendRichMessage', 'sendMessage'), [])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)

    async def test_unchanged_body_heartbeat_does_not_create_or_edit_messages(self):
        self.output()
        await self.update()
        first = self.drafts()[0]['draft_id']
        self.now += 14
        await self.update()
        self.assertEqual(len(self.drafts()), 1)
        self.now += 1
        await self.update()
        self.restart()
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.assertEqual(self.calls_for('editMessageText'), [])
        self.assertEqual([p['draft_id'] for p in self.drafts()], [first] * 3)

    async def test_new_text_edits_the_same_body_before_redrawing_status(self):
        self.output('正文一')
        await self.update()
        draft_id = self.drafts()[-1]['draft_id']
        mid = self.deliveries['a'].body_preview.load(self.identity())['message_id']
        self.output('正文二', iid='second')
        self.now += 15
        await self.update()
        edits = self.calls_for('editMessageText')
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0][1]['message_id'], mid)
        self.assertIn('正文一', self.body_text(edits[0][1]))
        self.assertIn('正文二', self.body_text(edits[0][1]))
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.assertNotIn('正文二', str(self.drafts()[-1]['rich_message']))
        self.assertEqual(self.drafts()[-1]['draft_id'], draft_id)

    async def test_body_before_first_status_keeps_initial_task_draft_id(self):
        delivery = self.deliveries['a']
        delivery.watch(self.turn())
        initial_id = self.identity()['draft_id']
        self.output('先有正文，再第一次显示状态')
        await self.update()
        self.assertEqual(self.drafts()[0]['draft_id'], initial_id)
        self.assertEqual(len(self.drafts()), 1)
        self.now += 15
        await self.update()
        self.assertEqual(self.drafts()[-1]['draft_id'], initial_id)

    async def test_new_telegram_input_moves_status_once_without_recreating_body(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        self.output()
        await self.update()
        original_id = self.drafts()[-1]['draft_id']
        store.accept({'update_id': 9}, {'message_id': 150, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '补充一项要求'})
        store.mark(9, 'running', 'a-work')
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1001000 WHERE id=9')
        self.now += 1
        await delivery.receipt(self.telegram)
        moved_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(moved_id, original_id)
        self.assertEqual(self.identity()['draft_id'], moved_id)
        self.assertEqual(delivery._receipt_state()['draft_id'], moved_id)
        self.now += 15
        await delivery.receipt(self.telegram)
        self.assertEqual(self.drafts()[-1]['draft_id'], moved_id)
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.assertEqual(self.calls_for('editMessageText'), [])

    async def test_messages_in_other_chats_or_topics_do_not_move_this_status(self):
        store = self.bridges['a'].store
        self.output()
        await self.update()
        original_id = self.drafts()[-1]['draft_id']
        store.enqueue_at('input:90:reply', '999', '其他接收人', 42)
        store.enqueue_at('input:91:reply', '123', '其他话题', 420)
        with store.db:
            store.db.execute("UPDATE outbox SET message_id=10000 WHERE chat_id='999'")
            store.db.execute('UPDATE outbox SET message_id=10001 WHERE message_thread_id=420')
        for uid, owner, topic in ((90, 999, 42), (91, 123, 420)):
            store.accept({'update_id': uid}, {'message_id': 10000 + uid, 'chat': {'id': owner},
                                            'message_thread_id': topic, 'text': '其他对话的消息'})
        self.now += 15
        await self.update()
        self.assertEqual(self.drafts()[-1]['draft_id'], original_id)
        self.assertEqual(self.calls_for('editMessageText'), [])

    async def test_rotated_status_id_survives_rate_limit_and_restart_for_the_same_tail(self):
        self.output()
        await self.update()
        original_id = self.drafts()[-1]['draft_id']
        await self.control_reply()
        self.failures['sendRichMessageDraft'] = [ApiError(429, retry_after=30)]
        self.now += 1
        await self.deliveries['a'].receipt(self.telegram)
        attempted_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(attempted_id, original_id)
        self.assertEqual(self.identity()['draft_id'], attempted_id)
        self.restart()
        self.now += 15
        await self.deliveries['a'].receipt(self.telegram)
        self.assertEqual(len(self.drafts()), 2)
        self.now += 15
        await self.deliveries['a'].receipt(self.telegram)
        self.assertEqual([p['draft_id'] for p in self.drafts()], [original_id, attempted_id, attempted_id])

    async def test_rotation_is_persisted_before_api_and_unknown_ack_reuses_id_after_restart(self):
        self.output()
        await self.update()
        original_id = self.drafts()[-1]['draft_id']
        store = self.bridges['a'].store
        store.accept({'update_id': 9}, {'message_id': 150, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '补充要求'})
        store.mark(9, 'running', 'a-work')
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1001000 WHERE id=9')
        original_call = self.telegram.call
        persisted = []

        async def inspect_before_api(method, **params):
            if method == 'sendRichMessageDraft':
                delivery = self.deliveries['a']
                persisted.append((self.identity()['draft_id'], delivery._receipt_state()['draft_id']))
                self.assertEqual(persisted[-1], (params['draft_id'], params['draft_id']))
            return await original_call(method, **params)

        self.telegram.call = inspect_before_api
        self.failures['sendRichMessageDraft'] = [ConnectionError('lost status acknowledgement')]
        self.now += 1
        await self.deliveries['a'].receipt(self.telegram)
        attempted_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(attempted_id, original_id)
        self.restart()
        self.now += 15
        await self.deliveries['a'].receipt(self.telegram)
        self.assertEqual(self.drafts()[-1]['draft_id'], attempted_id)
        self.assertEqual(persisted, [(attempted_id, attempted_id)] * 2)

    async def test_legacy_ack_without_position_tail_gets_one_repair_id(self):
        delivery = self.deliveries['a']
        delivery.watch(self.turn())
        self.output()
        identity = self.identity()
        await delivery.body_preview.update(self.telegram, identity, '第一段正文')
        original_id = identity['draft_id']
        state = delivery.state()
        state['status_draft_ack'] = {'route': delivery.route(), 'turn_id': 'a-work',
                                     'draft_id': original_id, 'text': '旧版状态', 'sent_at': self.now}
        delivery.store.set(delivery.KEY, json.dumps(state))
        self.restart()
        await self.update()
        repaired_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(repaired_id, original_id)
        self.now += 15
        self.restart()
        await self.update()
        self.assertEqual(self.drafts()[-1]['draft_id'], repaired_id)

    async def test_injected_and_out_of_range_message_ids_cannot_freeze_real_message_positions(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        for uid, mid in ((-90, 2_000_000_000), (90, 1_793_896_675_577_860)):
            store.accept({'update_id': uid}, {'message_id': mid, 'chat': {'id': 123},
                                            'message_thread_id': 42, 'text': '历史注入任务'})
            store.mark(uid, 'done')
        store.enqueue_at('input:90:reply', '123', '历史注入回复', 42)
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=900000')
            store.db.execute('UPDATE outbox SET message_id=1793896675577861')
        store.accept({'update_id': 7}, {'message_id': 7, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '本轮真实 Telegram 任务'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1000000 WHERE id=7')
        await delivery.receipt(self.telegram)
        receipt_id = self.drafts()[-1]['draft_id']
        self.assertEqual(delivery.state()['status_draft_ack']['position_tail'], 7)
        store.mark(7, 'running', 'a-work')
        self.output('真实正文')
        self.now += 1
        await self.update()
        body_id = delivery.body_preview.load(self.identity())['message_id']
        self.assertNotEqual(self.drafts()[-1]['draft_id'], receipt_id)
        self.assertEqual(delivery.state()['status_draft_ack']['position_tail'], body_id)
        moved_id = self.drafts()[-1]['draft_id']
        store.accept({'update_id': 9}, {'message_id': 150, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '下一条真实消息'})
        store.mark(9, 'running', 'a-work')
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1002000 WHERE id=9')
        self.now += 1
        await delivery.receipt(self.telegram)
        self.assertNotEqual(self.drafts()[-1]['draft_id'], moved_id)
        self.assertEqual(delivery.state()['status_draft_ack']['position_tail'], 150)

    async def test_polluted_v11_tail_repairs_once_then_real_input_can_reanchor_again(self):
        self.output()
        await self.update()
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        original_id = self.drafts()[-1]['draft_id']
        key, value = store.db.execute('SELECT key,value FROM kv WHERE key LIKE ?',
                                     (StatusDraftPosition.KEY + '%',)).fetchone()
        polluted = json.loads(value)
        polluted['position_tail'] = 1_793_896_675_577_860
        store.set(key, json.dumps(polluted))
        old_state = delivery.state()
        old_state['status_draft_ack']['position_tail'] = polluted['position_tail']
        store.set(delivery.KEY, json.dumps(old_state))
        self.failures['sendRichMessageDraft'] = [ConnectionError('repair acknowledgement lost')]
        self.restart()
        self.now += 15
        await self.update()
        repaired_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(repaired_id, original_id)
        position = json.loads(store.get(key))
        body_id = self.deliveries['a'].body_preview.load(self.identity())['message_id']
        self.assertEqual(position['position_tail'], body_id)
        self.restart()
        self.now += 15
        await self.update()
        self.assertEqual(self.drafts()[-1]['draft_id'], repaired_id)
        store.accept({'update_id': 9}, {'message_id': 150, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '修复后的真实新消息'})
        store.mark(9, 'running', 'a-work')
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1031000 WHERE id=9')
        self.now += 1
        await self.deliveries['a'].receipt(self.telegram)
        moved_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(moved_id, repaired_id)
        self.assertEqual(self.deliveries['a'].state()['status_draft_ack']['position_tail'], 150)
        self.restart()
        self.now += 15
        await self.deliveries['a'].receipt(self.telegram)
        self.assertEqual(self.drafts()[-1]['draft_id'], moved_id)

    async def test_final_output_reanchors_terminal_status_once_without_resending_final(self):
        delivery = self.deliveries['a']
        self.output()
        await self.update()
        original_id = self.drafts()[-1]['draft_id']
        delivery.seal(self.turn(status='completed'))
        final = delivery.next_row()
        await self.acknowledge(final)
        terminal_id = self.drafts()[-1]['draft_id']
        self.assertNotEqual(terminal_id, original_id)
        self.assertIn('已完成', str(self.drafts()[-1]['rich_message']))
        self.restart()
        await self.deliveries['a'].complete_status(self.telegram, final)
        self.assertEqual(self.drafts()[-1]['draft_id'], terminal_id)
        finals = [p for _, p in self.calls_for('sendRichMessage', 'sendMessage')
                  if p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 1)

    async def test_fast_pending_input_after_final_gets_fresh_status_before_native_turn(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        store.accept({'update_id': 7}, {'message_id': 7, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '任务 A'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1000000 WHERE id=7')
        await delivery.receipt(self.telegram)
        store.mark(7, 'running', 'a-work')
        self.output('任务 A 的正文')
        self.now += 1
        await self.update()
        delivery.seal(self.turn(status='completed'))
        await self.acknowledge(delivery.next_row())
        final_a_draft = self.drafts()[-1]['draft_id']
        store.mark(7, 'done', 'a-work')
        self.assertEqual(delivery._receipt_state()['source_id'], 7)
        sends_before_b = len(self.calls_for('sendRichMessage', 'sendMessage'))
        store.accept({'update_id': 9}, {'message_id': 150, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '紧接着开始任务 B'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1002000 WHERE id=9')
        self.now += 1
        await delivery.receipt(self.telegram)
        pending_b_draft = self.drafts()[-1]['draft_id']
        self.assertNotEqual(pending_b_draft, final_a_draft)
        self.assertEqual(delivery._receipt_state()['source_id'], 9)
        self.assertEqual(delivery._receipt_state()['draft_id'], pending_b_draft)
        self.assertIn('已收到', str(self.drafts()[-1]['rich_message']))
        self.assertNotIn('已完成', str(self.drafts()[-1]['rich_message']))
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), sends_before_b)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 1)

    async def test_unknown_body_create_ack_never_resends_after_restart(self):
        self.failures['sendRichMessage'] = [ConnectionError('lost creation acknowledgement')]
        self.output()
        await self.update()
        self.restart()
        self.now += 15
        self.output('仍能保存的新过程', iid='second')
        await self.update()
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.assertEqual(self.calls_for('editMessageText', 'deleteMessage'), [])
        self.assertEqual(len(self.drafts()), 2)
        self.deliveries['a'].seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertIn('仍能保存的新过程', ''.join(row[1] for row in rows))
        self.assertEqual(sum(not row[2] for row in rows), 1)

    async def test_body_create_rate_limit_does_not_suppress_status_and_retries_once_due(self):
        self.failures['sendRichMessage'] = [ApiError(429, retry_after=30)]
        self.output()
        await self.update()
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)
        self.assertEqual(len(self.drafts()), 2)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 2)
        self.assertEqual(len(self.drafts()), 3)

    async def test_body_edit_rate_limit_keeps_the_status_draft_updating(self):
        self.output()
        await self.update()
        self.failures['editMessageText'] = [ApiError(429, retry_after=30)]
        self.output('新增正文', iid='second')
        self.now += 15
        await self.update()
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls_for('editMessageText')), 1)
        self.assertEqual(len(self.drafts()), 3)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls_for('editMessageText')), 2)
        self.assertEqual(len(self.calls_for('sendRichMessage', 'sendMessage')), 1)

    async def test_next_task_uses_a_new_body_without_editing_completed_history(self):
        self.output()
        await self.update()
        first_mid = self.deliveries['a'].body_preview.load(self.identity())['message_id']
        self.deliveries['a'].seal(self.turn(status='completed'))
        await self.drain()
        self.now += 15
        self.output('下一轮正文', tid='a-next')
        await self.update(tid='a-next')
        next_mid = self.deliveries['a'].body_preview.load(self.identity('a-next'))['message_id']
        self.assertNotEqual(next_mid, first_mid)
        self.assertNotIn(first_mid, [p['message_id'] for _, p in self.calls_for('editMessageText')])

    async def test_long_unicode_body_is_bounded_without_losing_full_final_output(self):
        body = '原始开头\n' + '😀过程内容' * 1800 + '\n原始结尾'
        self.output(body)
        await self.update()
        preview = self.body_text(self.calls_for('sendRichMessage', 'sendMessage')[0][1])
        self.assertLessEqual(utf16_size(preview), 3800)
        self.assertIn('原始结尾', preview)
        stored = self.bridges['a'].store.db.execute("SELECT text FROM outbox WHERE key LIKE 'turn:%'").fetchall()
        self.assertEqual(''.join(row[0] for row in stored), body)
        self.deliveries['a'].seal(self.turn(status='completed'))
        rows = await self.drain()
        final = ''.join(row[1] for row in rows)
        self.assertEqual(final.count('😀'), body.count('😀'))
        self.assertTrue(final.startswith('原始开头'))
        self.assertIn('原始结尾', final)
        self.assertEqual(sum(not row[2] for row in rows), 1)

    async def test_cleanup_waits_for_final_ack_and_delete_retry_does_not_resend_final(self):
        delivery = self.deliveries['a']
        self.output()
        await self.update()
        mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed'))
        row = delivery.next_row()
        delivery.begin(row)
        await delivery.complete_status(self.telegram, row)
        self.assertEqual(self.calls_for('deleteMessage'), [])
        self.failures['deleteMessage'] = [ApiError(429, retry_after=30)]
        await self.acknowledge(row)
        self.assertEqual(len(self.calls_for('deleteMessage')), 1)
        self.assertIsNone(delivery.next_row())
        self.restart()
        self.now += 30
        await self.update(status='completed')
        self.assertEqual([p['message_id'] for _, p in self.calls_for('deleteMessage')], [mid, mid])
        self.assertNotIn(mid, self.messages)
        self.assertIsNone(self.deliveries['a'].next_row())
        finals = [p for _, p in self.calls_for('sendRichMessage', 'sendMessage')
                  if p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 1)

    async def test_restart_recovers_cleanup_after_final_ack_before_finish_hook(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        self.output()
        await self.update()
        mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed'))
        row = delivery.next_row()
        delivery.begin(row)
        store.images.begin(row)
        sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'],
                                 rich_text=row['rich_text'], topic_id=row['message_thread_id'],
                                 disable_notification=delivery.silent(row))
        store.images.sent(row, sent['message_id'])
        delivery.sent(row, sent['message_id'])
        # Simulate process loss before complete_status()/finish() runs.
        self.assertEqual(self.calls_for('deleteMessage'), [])
        self.restart()
        self.now += 15
        await self.update(status='completed')
        self.assertEqual([p['message_id'] for _, p in self.calls_for('deleteMessage')], [mid])
        self.assertIsNone(self.deliveries['a'].next_row())
        finals = [p for _, p in self.calls_for('sendRichMessage', 'sendMessage')
                  if p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 1)

    async def test_failed_and_stopped_tasks_keep_one_final_notice_and_persistent_footer(self):
        for tid, status, expected in (('a-failed', 'failed', '失败'), ('a-stop', 'interrupted', '停止')):
            with self.subTest(status=status):
                self.now += 15
                self.output('已保存的过程', tid=tid)
                await self.update(tid=tid)
                self.deliveries['a'].seal(self.turn(tid, status))
                rows = await self.drain()
                self.assertEqual(sum(not row[2] for row in rows), 1)
                self.assertIn('已保存的过程', ''.join(row[1] for row in rows))
                self.assertIn(expected, ''.join(row[1] for row in rows))

    async def test_slow_body_edit_in_one_topic_does_not_block_another_status(self):
        self.output()
        self.output('B正文', tid='b-work', name='b')
        await asyncio.gather(self.update(), self.update('b'))
        mid = self.deliveries['a'].body_preview.load(self.identity())['message_id']
        entered, release = asyncio.Event(), asyncio.Event()

        async def block_a(method, params):
            if method == 'editMessageText' and params['message_id'] == mid:
                entered.set()
                await release.wait()

        self.after_call = block_a
        self.output('A慢更新', iid='second')
        self.now += 15
        task = asyncio.create_task(self.update())
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            await asyncio.wait_for(self.update('b'), 0.5)
            self.assertFalse(task.done())
            self.assertEqual(self.drafts()[-1]['message_thread_id'], 84)
        finally:
            release.set()
            await task

    async def test_owner_revoked_during_body_create_prevents_following_status(self):
        async def revoke(method, params):
            if method in ('sendMessage', 'sendRichMessage'):
                self.bridges['a'].owner_allowed = lambda: False

        self.after_call = revoke
        self.output()
        await self.update()
        self.assertEqual(self.drafts(), [])
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls), before)

    async def test_repaired_topic_never_deletes_the_old_routes_preview(self):
        delivery, bridge = self.deliveries['a'], self.bridges['a']
        self.output()
        await self.update()
        identity = self.identity()
        delivery.body_preview.finish(identity)
        bridge.target['message_thread_id'] = 420
        await delivery.body_preview.cleanup(self.telegram)
        self.assertEqual(self.calls_for('deleteMessage'), [])
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.calls), before)

    async def test_old_completion_cleans_only_its_body_without_finishing_new_input(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        self.output()
        await self.update()
        old_mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed'))
        row = delivery.next_row()
        store.accept({'update_id': 9}, {'message_id': 9, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '下一项任务'})
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1002000 WHERE id=9')
        self.now += 2
        await delivery.receipt(self.telegram)
        await self.acknowledge(row)
        self.assertIn(old_mid, [p['message_id'] for _, p in self.calls_for('deleteMessage')])
        latest_status = self.drafts()[-1]['rich_message']['blocks'][0]['text']
        self.assertNotIn('✅ 已完成', latest_status)

    async def test_late_native_only_completion_cannot_replace_newer_native_task_status(self):
        delivery, store = self.deliveries['a'], self.bridges['a'].store
        self.output('任务 A 的过程')
        await self.update()
        old_mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed'))
        final_a = delivery.next_row()
        self.now = 1030
        self.output('任务 B 的过程', tid='a-next')
        await delivery.update(self.telegram, turns=[self.turn('a-next', started=1029)])
        next_identity = self.identity('a-next')
        next_mid = delivery.body_preview.load(next_identity)['message_id']
        next_draft_id = self.drafts()[-1]['draft_id']
        next_status = copy.deepcopy(self.drafts()[-1])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
        self.assertIn('正在工作', str(next_status['rich_message']))
        await self.acknowledge(final_a)
        self.assertIn(old_mid, [p['message_id'] for _, p in self.calls_for('deleteMessage')])
        self.assertNotIn(old_mid, self.messages)
        self.assertIn(next_mid, self.messages)
        self.assertEqual(self.drafts()[-1], next_status)
        self.assertEqual(self.drafts()[-1]['draft_id'], next_draft_id)
        self.assertNotIn('✅ 已完成', str(self.drafts()[-1]['rich_message']))
        self.assertIsNone(delivery.next_row(), 'A must not be delivered again while B is active')
        finals = [p for _, p in self.calls_for('sendRichMessage', 'sendMessage')
                  if p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 1)

    async def test_same_timestamp_native_turns_use_creation_order_for_status_and_completion(self):
        delivery = self.deliveries['a']
        self.output('同一秒开始的任务 A')
        await delivery.update(self.telegram, turns=[self.turn(started=1001)])
        old_mid = delivery.body_preview.load(self.identity())['message_id']
        delivery.seal(self.turn(status='completed', started=1001))
        final_a = delivery.next_row()
        self.output('同一秒开始的任务 B', tid='a-next')
        self.now += 15
        await delivery.update(self.telegram, turns=[self.turn('a-next', started=1001)])
        next_mid = delivery.body_preview.load(self.identity('a-next'))['message_id']
        next_status = copy.deepcopy(self.drafts()[-1])
        self.assertIn('正在工作', str(next_status['rich_message']))
        self.assertNotEqual(next_mid, old_mid)
        await self.acknowledge(final_a)
        self.assertNotIn(old_mid, self.messages)
        self.assertIn(next_mid, self.messages)
        self.assertEqual(self.drafts()[-1], next_status)
        delivery.seal(self.turn('a-next', status='completed', started=1001))
        final_b = delivery.next_row()
        await self.acknowledge(final_b)
        self.assertNotIn(next_mid, self.messages)
        self.assertIn('已完成', str(self.drafts()[-1]['rich_message']))
        self.assertIsNone(delivery.next_row())
        finals = [p for _, p in self.calls_for('sendRichMessage', 'sendMessage')
                  if p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 2)

    async def test_explicit_plain_mode_keeps_body_in_draft_and_creates_no_preview_message(self):
        self.bridges['a'].config['preview_style'] = 'plain'
        self.restart()
        self.output('旧模式正文')
        await self.update()
        self.assertEqual(self.calls_for('sendRichMessage', 'sendMessage', 'editMessageText'), [])
        drafts = self.calls_for('sendMessageDraft')
        self.assertEqual(len(drafts), 1)
        self.assertIn('旧模式正文', drafts[0][1]['text'])
