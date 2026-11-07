import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from completion_delivery import CompletionDelivery
from message_format import utf16_size
from topics_bridge import RoutedStore


class ApiError(Exception):
    def __init__(self, code, retry_after=3):
        self.code, self.retry_after = code, retry_after


class CompletionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RoutedStore(Path(self.tmp.name) / 'bridge.sqlite', 'native')
        self.now = 1000
        self.bridge = SimpleNamespace(store=self.store, thread_id='native', owner='123',
                                      target={'key': 'target', 'thread_id': 'native', 'message_thread_id': 42},
                                      config={'notification_mode': 'completion'}, owner_allowed=lambda: True)
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.calls, self.error = [], None
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, params))
                if test.error:
                    raise test.error
                return True

        self.telegram = Telegram()

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def turn(self, status='inProgress', tid='work', started=1001):
        return {'id': tid, 'status': status, 'startedAt': started}

    def output(self, text='阶段一', key='turn:work:first', topic=42, chat='123'):
        self.store.enqueue_at(key, chat, text, topic)

    def mark_sent(self, row, mid=10):
        self.store.images.sent(row, mid)
        self.delivery.sent(row, mid)

    def all_outbox(self):
        return self.store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    async def test_disabled_mode_preserves_immediate_delivery(self):
        self.bridge.config = {}
        delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.output()
        self.assertIsNotNone(delivery.next_row())
        self.assertFalse(delivery.silent(delivery.next_row()))
        await delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls, [])

    async def test_active_output_is_held_while_explicit_command_reply_bypasses_it(self):
        self.delivery.watch(self.turn())
        self.output()
        self.output('/status 查询结果', 'input:5:reply')
        row = self.delivery.next_row()
        self.assertTrue(row['key'].startswith('input:5:'))
        self.assertFalse(self.delivery.silent(row))
        self.mark_sent(row)
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(len(self.all_outbox()), 2)

    async def test_retry_keeps_a_turns_output_order_without_blocking_explicit_controls(self):
        self.delivery.watch(self.turn())
        self.output('第一段等待限流重试', 'turn:work:first')
        self.output('第二段不能提前发送', 'turn:work:second')
        self.output('独立的状态查询结果', 'input:7:reply')
        self.delivery.seal(self.turn('completed'))
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET retry_at=? WHERE key=?',
                                  (self.now + 20, self.all_outbox()[0]['key']))
        command = self.delivery.next_row()
        self.assertEqual(command['text'], '独立的状态查询结果')
        self.mark_sent(command)
        self.assertIsNone(self.delivery.next_row())
        self.now += 20
        first = self.delivery.next_row()
        self.assertEqual(first['text'], '第一段等待限流重试')
        self.mark_sent(first)
        self.assertEqual(self.delivery.next_row()['text'], '第二段不能提前发送')

    async def test_terminal_event_does_not_release_before_full_scan_seal(self):
        self.delivery.watch(self.turn())
        self.output()
        self.delivery.terminal(self.turn('completed'))
        self.assertTrue(self.delivery.needs_scan('work'))
        self.assertIsNone(self.delivery.next_row())
        self.assertFalse(self.delivery.seal(self.turn()))
        self.assertIsNone(self.delivery.next_row())
        self.assertTrue(self.delivery.seal(self.turn('completed')))
        row = self.delivery.next_row()
        self.assertEqual(row['text'], '阶段一')
        self.assertTrue(self.delivery.silent(row))
        self.mark_sent(row)
        notice = self.delivery.next_row()
        self.assertTrue(notice['key'].startswith('completion-notice:'))
        self.assertFalse(self.delivery.silent(notice))
        self.delivery.begin(notice)
        self.mark_sent(notice)
        self.assertIsNone(self.delivery.next_row())
        self.delivery.seal(self.turn('completed'))
        self.assertIsNone(self.delivery.next_row())

    async def test_cumulative_draft_has_status_and_all_previous_text_with_stable_id(self):
        self.delivery.watch(self.turn())
        self.output('第一段保留')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        first = self.calls[-1][1]
        self.assertIn('正在工作', first['text'])
        self.assertTrue(first['text'].endswith('第一段保留'))
        self.output('第二段新增', 'turn:work:second')
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        last = self.calls[-1][1]
        self.assertEqual(first['draft_id'], last['draft_id'])
        self.assertEqual(last['message_thread_id'], 42)
        self.assertEqual(last['chat_id'], 123)
        self.assertIn('第一段保留\n\n第二段新增', last['text'])
        self.assertTrue(all(method == 'sendMessageDraft' for method, _ in self.calls))
        self.assertEqual(len(self.all_outbox()), 2)

    async def test_unchanged_preview_refreshes_only_every_fifteen_seconds(self):
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.now += 14
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1]['draft_id'], self.calls[1][1]['draft_id'])

    async def test_preview_limit_keeps_full_durable_body_and_marks_older_omission(self):
        body = '原始开头\n' + '😀过程内容' * 1800 + '\n原始结尾'
        self.output(body)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        preview = self.calls[-1][1]['text']
        self.assertLessEqual(utf16_size(preview), 3800)
        self.assertIn('预览仅显示最近部分', preview)
        self.assertTrue(preview.endswith('原始结尾'))
        self.assertEqual(''.join(r['text'] for r in self.all_outbox()), body)
        self.assertTrue(all(r['message_id'] is None for r in self.all_outbox()))

    async def test_unsupported_drafts_do_not_fall_back_to_ordinary_messages(self):
        self.error = ApiError(404)
        self.output()
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.now += 30
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'sendMessageDraft')
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(len(self.all_outbox()), 1)

    async def test_disconnection_and_waiting_are_status_updates_not_completion(self):
        await self.delivery.update(self.telegram, turns=[self.turn()], status={'activeFlags': ['waitingOnApproval']})
        self.assertIn('需要你处理', self.calls[-1][1]['text'])
        self.now += 15
        await self.delivery.update(self.telegram, connected=False)
        self.assertIn('连接中断', self.calls[-1][1]['text'])
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(self.all_outbox(), [])

    async def test_old_completed_history_does_not_notify_or_preview(self):
        await self.delivery.update(self.telegram, turns=[self.turn('completed', started=900)])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.delivery.next_row())
        self.output('更新前已排队的结果')
        self.delivery.watch(self.turn('completed', started=900))
        self.assertTrue(self.delivery.needs_scan('work'))
        self.delivery.seal(self.turn('completed', started=900))
        row = self.delivery.next_row()
        self.assertEqual(row['text'], '更新前已排队的结果')
        self.mark_sent(row)
        self.assertIsNone(self.delivery.next_row())
        await self.delivery.update(self.telegram, turns=[self.turn('completed', started=900)])
        self.assertEqual(self.calls, [])

    async def test_active_turn_at_enable_can_finish_with_one_notice(self):
        self.delivery.watch(self.turn(started=900))
        self.delivery.seal(self.turn('completed', started=900))
        notice = self.delivery.next_row()
        self.assertTrue(notice['key'].startswith('completion-notice:'))
        self.mark_sent(notice)
        self.assertIsNone(self.delivery.next_row())

    async def test_same_mode_restart_preserves_window_but_reenable_does_not_alert_off_history(self):
        original_since = self.delivery.state()['since']
        self.delivery.watch(self.turn())
        self.output('关闭通知模式前已保存的过程')
        self.delivery.watch(self.turn(tid='still-active'))
        self.now += 20
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.assertEqual(self.delivery.state()['since'], original_since)
        self.bridge.config['notification_mode'] = 'all'
        disabled = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.assertFalse(disabled.state()['enabled'])
        self.now += 20
        self.output('关闭期间已完成的输出', 'turn:off-history:answer')
        self.bridge.config['notification_mode'] = 'completion'
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.assertEqual(self.delivery.state()['since'], self.now)
        self.delivery.watch(self.turn('completed'))
        self.delivery.seal(self.turn('completed'))
        off_turn = self.turn('completed', tid='off-history', started=self.now - 5)
        self.delivery.watch(off_turn)
        self.delivery.seal(off_turn)
        for _ in range(2):
            row = self.delivery.next_row()
            self.assertFalse(row['key'].startswith('completion-notice:'))
            self.mark_sent(row)
        self.assertIsNone(self.delivery.next_row())
        self.delivery.watch(self.turn(tid='still-active'))
        self.delivery.seal(self.turn('completed', tid='still-active'))
        self.assertTrue(self.delivery.next_row()['key'].startswith('completion-notice:'))

    async def test_reenable_suppresses_an_old_queued_unnoticed_completion(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.bridge.config['notification_mode'] = 'all'
        CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 15
        self.bridge.config['notification_mode'] = 'completion'
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.assertIsNone(self.delivery.next_row())
        saved = self.store.db.execute('SELECT message_id FROM outbox WHERE key=?', (notice['key'],)).fetchone()
        self.assertEqual(saved[0], -1)
        self.assertEqual(self.store.db.execute('SELECT notice_state FROM completion_turns').fetchone()[0], 'suppressed')

    async def test_all_input_copy_waits_for_its_exact_native_turn(self):
        self.store.accept({'update_id': 5}, {'chat': {'id': 123}, 'message_id': 5,
                                           'message_thread_id': 42, 'text': '原输入'})
        self.output('📩 原输入', 'main-guard-input:draft:5')
        self.assertIsNone(self.delivery.next_row())
        self.store.mark(5, 'running', 'work')
        self.delivery.watch(self.turn())
        self.assertIsNone(self.delivery.next_row())
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertIn('📩 原输入', self.calls[-1][1]['text'])
        self.delivery.seal(self.turn('completed'))
        row = self.delivery.next_row()
        self.assertTrue(row['key'].startswith('main-guard-input:'))
        self.assertTrue(self.delivery.silent(row))

    async def test_approval_notice_remains_frozen_to_original_turn(self):
        self.delivery.watch(self.turn())
        self.output('请返回 Codex 处理', 'request:rpc-1')
        self.assertIsNone(self.delivery.next_row())
        self.delivery.seal(self.turn('completed'))
        self.delivery.watch(self.turn(tid='second'))
        row = self.delivery.next_row()
        self.assertTrue(row['key'].startswith('request:rpc-1'))
        self.assertTrue(self.delivery.silent(row))

    async def test_images_and_late_error_messages_are_processed_before_completion_notice(self):
        self.delivery.watch(self.turn())
        with self.store.db:
            self.store.db.execute('INSERT INTO outbox(key,chat_id,text,media_json,message_thread_id) VALUES(?,?,?,?,?)',
                                  ('turn:work:image:topic:42:media:0', '123', '', json.dumps({'caption': '结果图'}), 42))
        self.delivery.seal(self.turn('completed'))
        image = self.delivery.next_row()
        self.assertTrue(self.delivery.silent(image))
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (image['key'],))
        self.output('图片结果尚未确认', 'image-error:' + image['key'])
        error = self.delivery.next_row()
        self.assertTrue(error['key'].startswith('image-error:'))
        self.assertTrue(self.delivery.silent(error))
        self.mark_sent(error)
        self.assertTrue(self.delivery.next_row()['key'].startswith('completion-notice:'))

    async def test_late_content_precedes_already_queued_notice(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.output('迟到的最终片段', 'turn:work:late')
        content = self.delivery.next_row()
        self.assertEqual(content['text'], '迟到的最终片段')
        self.mark_sent(content)
        self.assertEqual(self.delivery.next_row()['key'], notice['key'])

    async def test_stale_active_snapshot_cannot_regress_a_terminal_or_ready_turn(self):
        self.delivery.watch(self.turn())
        self.delivery.terminal(self.turn('completed'))
        self.delivery.watch(self.turn())
        self.assertTrue(self.delivery.needs_scan('work'))
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.delivery.watch(self.turn())
        self.assertEqual(self.store.db.execute('SELECT status FROM completion_turns').fetchone()[0], 'completed')
        self.assertEqual(self.delivery.next_row()['key'], notice['key'])
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertIn('已完成', self.calls[-1][1]['text'])

    async def test_lost_completion_response_is_not_automatically_resent(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.delivery.begin(notice)
        self.assertTrue(self.delivery.failed(notice, ConnectionError('lost response')))
        self.assertIsNone(self.delivery.next_row())
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.delivery.recover()
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(self.store.db.execute('SELECT notice_state FROM completion_turns').fetchone()[0], 'uncertain')

    async def test_crash_during_completion_send_is_uncertain_after_restart(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.delivery.begin(notice)
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.delivery.recover()
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(self.store.db.execute('SELECT message_id FROM outbox').fetchone()[0], -1)

    async def test_rate_limited_completion_retries_without_blocking_commands(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.delivery.begin(notice)
        self.assertTrue(self.delivery.failed(notice, ApiError(429, retry_after=20)))
        self.output('参数查询', 'input:6:reply')
        command = self.delivery.next_row()
        self.assertEqual(command['text'], '参数查询')
        self.mark_sent(command)
        self.assertIsNone(self.delivery.next_row())
        self.now += 20
        self.assertEqual(self.delivery.next_row()['key'], notice['key'])

    async def test_repaired_topic_does_not_retarget_old_outputs_or_notifications(self):
        self.delivery.watch(self.turn())
        self.output('原话题输出')
        self.delivery.seal(self.turn('completed'))
        self.bridge.target['message_thread_id'] = 99
        self.assertIsNone(self.delivery.next_row())
        row = self.all_outbox()[0]
        self.assertEqual(row['message_thread_id'], 42)
        self.assertEqual(row['message_id'], -1)
        self.assertFalse(any(r['key'].startswith('completion-notice:') for r in self.all_outbox()))

    async def test_repair_during_active_turn_cannot_claim_it_as_new_topic_work(self):
        self.delivery.watch(self.turn())
        self.output('原话题进行中的输出')
        self.delivery.next_row()
        self.bridge.target['message_thread_id'] = 99
        self.delivery.watch(self.turn())
        self.delivery.terminal(self.turn('completed'))
        self.assertFalse(self.delivery.needs_scan('work'))
        self.assertFalse(self.delivery.seal(self.turn('completed')))
        self.output('不能自动改投新话题的同一任务', 'turn:work:later', topic=99)
        self.assertIsNone(self.delivery.next_row())
        await self.delivery.update(self.telegram, turns=[self.turn('completed')])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 1)
        self.assertTrue(all(r['message_id'] == -1 for r in self.all_outbox()))

    async def test_restart_after_notice_outbox_commit_preserves_known_success(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('completed'))
        notice = self.delivery.next_row()
        self.delivery.begin(notice)
        self.store.images.sent(notice, 909)
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.delivery.recover()
        self.assertEqual(self.store.db.execute('SELECT notice_state FROM completion_turns').fetchone()[0], 'sent')
        self.assertEqual(self.all_outbox()[0]['message_id'], 909)
        self.assertIsNone(self.delivery.next_row())

    async def test_failed_and_interrupted_turns_have_distinct_terminal_notices(self):
        self.delivery.watch(self.turn())
        self.delivery.seal(self.turn('failed'))
        failed = self.delivery.next_row()
        self.assertIn('执行失败', failed['text'])
        self.mark_sent(failed)
        self.delivery.watch(self.turn(tid='stopped'))
        self.delivery.seal(self.turn('interrupted', tid='stopped'))
        stopped = self.delivery.next_row()
        self.assertIn('已停止', stopped['text'])

