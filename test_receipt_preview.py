import asyncio
import json
import unittest

from completion_delivery import CompletionDelivery
import test_completion_delivery as fixtures


class ReceiptPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CompletionDeliveryTests.asyncSetUp
    asyncTearDown = fixtures.CompletionDeliveryTests.asyncTearDown
    output = fixtures.CompletionDeliveryTests.output
    turn = fixtures.CompletionDeliveryTests.turn

    def accept(self, text='请处理任务', source_id=1, topic=42, chat=123, guard=None, **extra):
        message = {'chat': {'id': chat, 'type': 'private'}, 'message_thread_id': topic,
                   'message_id': source_id, 'text': text, **extra}
        self.store.accept({'update_id': source_id}, message, guard_binding=guard)
        with self.store.db:
            self.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?', (int(self.now * 1000), source_id))

    def text(self, index=-1):
        method, params = self.calls[index]
        return params['rich_message']['blocks'][0]['text'] if method == 'sendRichMessageDraft' else params['text']

    async def test_pending_input_gets_block_receipt_without_native_turn_or_output(self):
        self.bridge.config['preview_style'] = 'block'
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.accept()
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
        self.assertIn('已收到', self.text())
        self.assertIn('准备任务', self.text())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT state FROM inbox').fetchone()[0], 'pending')

    async def test_connection_and_dispatch_uncertainty_are_not_claimed_as_working(self):
        self.accept()
        await self.delivery.receipt(self.telegram, connected=False)
        self.assertIn('等待连接恢复', self.text())
        self.now += 15
        self.store.mark(1, 'dispatching')
        await self.delivery.receipt(self.telegram)
        self.assertIn('正在提交任务', self.text())
        self.now += 15
        self.store.mark(1, 'uncertain')
        await self.delivery.receipt(self.telegram)
        self.assertIn('核对投递状态', self.text())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)

    async def test_same_receipt_has_fifteen_second_heartbeat_and_no_busy_poll_sends(self):
        self.accept()
        await self.delivery.receipt(self.telegram)
        first = self.calls[-1][1]['draft_id']
        for offset in (1, 2, 3, 14):
            self.now = 1000 + offset
            self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.calls), 1)
        self.now = 1015
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls[-1][1]['draft_id'], first)

    async def test_new_input_acknowledges_before_fifteen_seconds_but_bursts_are_coalesced(self):
        self.accept()
        await self.delivery.receipt(self.telegram)
        first = self.calls[-1][1]['draft_id']
        self.now += 0.1
        self.accept('补充一', 2)
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.now += 0.1
        self.accept('补充二', 3)
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.now += 1
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls[-1][1]['draft_id'], first)
        self.assertEqual(self.delivery._receipt_state()['source_id'], 3)
        self.assertEqual(len(self.calls), 2)

    async def test_real_turn_adopts_receipt_id_and_retains_it_across_restart(self):
        self.accept()
        await self.delivery.receipt(self.telegram)
        first = self.calls[-1][1]['draft_id']
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        self.assertEqual(self.delivery._group('work')['draft_id'], first)
        self.output('首条回复之前已存在的公开进度')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls[-1][1]['draft_id'], first)
        self.assertIn('公开进度', self.text())
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 15
        await self.delivery.receipt(self.telegram)
        self.assertEqual(self.calls[-1][1]['draft_id'], first)
        self.assertIn('公开进度', self.text())

    async def test_running_input_can_show_a_first_receipt_before_maintenance(self):
        self.accept()
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertIn('已收到', self.text())
        self.assertEqual(self.calls[-1][1]['draft_id'], self.delivery._group('work')['draft_id'])

    async def test_supplementary_input_keeps_existing_cumulative_body_and_cached_waiting_status(self):
        self.delivery.watch(self.turn())
        self.output('必须保留的累计正文')
        await self.delivery.update(self.telegram, turns=[self.turn()], status={'activeFlags': ['waitingOnApproval']})
        first = self.calls[-1][1]['draft_id']
        self.now += 1
        self.accept('补充要求')
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls[-1][1]['draft_id'], first)
        self.assertIn('必须保留的累计正文', self.text())
        self.now += 15
        await self.delivery.receipt(self.telegram)
        self.assertIn('需要你处理', self.text())
        self.assertIn('必须保留的累计正文', self.text())

    async def test_commands_empty_messages_and_foreign_routes_do_not_create_receipts(self):
        for number, (text, topic, chat) in enumerate(
                [('/status', 42, 123), ('', 42, 123), ('其他话题', 99, 123), ('其他账号', 42, 999)], 100):
            self.accept(text, source_id=number, topic=topic, chat=chat)
            self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.delivery._receipt_state(), {})

    async def test_media_command_caption_is_task_input(self):
        self.accept('', caption='/stop 只是附件说明', photo=[{'file_id': 'fixture'}])
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertIn('已收到', self.text())
        self.assertEqual(self.store.db.execute('SELECT state FROM inbox').fetchone()[0], 'pending')

    async def test_guard_binding_change_and_access_revocation_clear_receipts(self):
        self.accept(guard={'draft_id': 'real-draft', 'target_key': 'target', 'thread_id': 'native', 'topic_id': 42})
        await self.delivery.receipt(self.telegram)
        self.now += 15
        self.bridge.owner_allowed = lambda: False
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.delivery._receipt_state(), {})
        self.bridge.owner_allowed = lambda: True
        self.bridge.target['message_thread_id'] = 43
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.calls), 1)

    async def test_invalid_guard_never_creates_a_receipt(self):
        self.accept(guard={'draft_id': 'real-draft', 'target_key': 'target', 'thread_id': 'different', 'topic_id': 42})
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls, [])

    async def test_failed_or_completed_latest_input_does_not_resurrect_older_receipt(self):
        self.accept('旧任务', 1)
        self.store.mark(1, 'running', 'older')
        self.now += 1
        self.accept('新任务', 2)
        await self.delivery.receipt(self.telegram)
        self.store.mark(2, 'failed')
        self.now += 15
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.delivery._receipt_state(), {})
        self.assertEqual(len(self.calls), 1)
        self.store.mark(2, 'done')
        self.now += 15
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.calls), 1)

    async def test_unsupported_receipt_does_not_fall_back_or_create_completion_notice(self):
        self.accept()
        self.error = fixtures.ApiError(404)
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.now += 30
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.assertIsNone(self.delivery.next_row())

    async def test_rate_limit_cooldown_applies_even_to_new_receipt_sources(self):
        self.accept()
        self.error = fixtures.ApiError(429, retry_after=40)
        await self.delivery.receipt(self.telegram)
        self.now += 10
        self.accept('新的补充', 2)
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.calls), 1)
        self.error = None
        self.now += 30
        self.assertTrue(await self.delivery.receipt(self.telegram))

    async def test_receipt_and_normal_preview_are_serialized_without_blocking_turn_tracking(self):
        self.accept()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, **params):
            self.calls.append((method, params))
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
            return True

        self.telegram.call = delayed
        receipt = asyncio.create_task(self.delivery.receipt(self.telegram))
        await entered.wait()
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        self.output('模型已经开始产生的进度')
        update = asyncio.create_task(self.delivery.update(self.telegram, turns=[self.turn()]))
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.delivery._group('work')['status'], 'inProgress')
        release.set()
        self.assertTrue(await receipt)
        await update
        self.assertEqual(len(self.calls), 2)
        self.assertIn('模型已经开始产生的进度', self.text())
        self.assertEqual(self.calls[0][1]['draft_id'], self.calls[1][1]['draft_id'])

    async def test_ready_previous_turn_cannot_replace_a_newer_pending_receipt(self):
        self.accept('任务 A', 1)
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        self.output('任务 A 已有结果')
        self.delivery.seal(self.turn('completed'))
        self.store.mark(1, 'done')
        self.now += 1
        self.accept('任务 B', 2)
        await self.delivery.receipt(self.telegram)
        self.assertIn('已收到', self.text())
        count = len(self.calls)
        self.now += 16
        await self.delivery.update(self.telegram, turns=[self.turn('completed')])
        self.assertEqual(len(self.calls), count)
        self.assertNotIn('已完成', self.text())

    async def test_waiting_status_cache_is_scoped_to_the_native_turn(self):
        self.accept('任务 A', 1)
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        await self.delivery.update(self.telegram, turns=[self.turn()], status={'activeFlags': ['waitingOnApproval']})
        self.assertIn('需要你处理', self.text())
        self.delivery.seal(self.turn('completed'))
        self.store.mark(1, 'done')
        self.now += 1
        self.accept('任务 B', 2)
        await self.delivery.receipt(self.telegram)
        self.store.mark(2, 'running', 'next')
        self.delivery.watch(self.turn(tid='next'))
        self.now += 15
        await self.delivery.receipt(self.telegram)
        self.assertNotIn('需要你处理', self.text())
        self.assertIn('正在工作', self.text())

    async def test_reschedule_receipt_restores_the_next_pending_preview_after_normal_delivery(self):
        self.accept()
        await self.delivery.receipt(self.telegram)
        before = self.delivery._receipt_state()
        since = self.delivery.state()['since']
        self.assertFalse(await self.delivery.receipt(self.telegram))
        self.delivery.reschedule_receipt()
        self.assertEqual(self.delivery._receipt_state()['draft_id'], before['draft_id'])
        self.assertEqual(self.delivery._receipt_state()['source_id'], before['source_id'])
        self.assertEqual(self.delivery.state()['since'], since)
        self.assertTrue(await self.delivery.receipt(self.telegram))

    async def test_reschedule_receipt_also_makes_an_existing_active_group_due(self):
        self.accept()
        await self.delivery.receipt(self.telegram)
        self.store.mark(1, 'running', 'work')
        self.delivery.watch(self.turn())
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertGreater(self.delivery._group('work')['next_preview'], self.now)
        self.delivery.reschedule_receipt()
        self.assertTrue(await self.delivery.receipt(self.telegram))

    async def test_structurally_invalid_json_does_not_crash_or_acknowledge_input(self):
        self.accept()
        examples = [('[]', None), ('"not-an-object"', None), ('{}', '[]'), ('{}', '"guard"'), ('{}', '{}')]
        for media, guard in examples:
            with self.subTest(media=media, guard=guard):
                with self.store.db:
                    self.store.db.execute('UPDATE inbox SET message_json=?,main_guard_json=? WHERE id=1', (media, guard))
                self.assertFalse(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls, [])

    async def test_old_topic_failure_cannot_mark_the_repaired_topic_unsupported(self):
        self.accept()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, **params):
            self.calls.append((method, params))
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
                raise fixtures.ApiError(400)
            return True

        self.telegram.call = delayed
        old = asyncio.create_task(self.delivery.receipt(self.telegram))
        await entered.wait()
        self.bridge.target['message_thread_id'] = 43
        self.now += 1
        self.accept('新话题中的任务', 2, topic=43)
        self.store.mark(2, 'running', 'next')
        self.delivery.watch(self.turn(tid='next'))
        release.set()
        self.assertFalse(await old)
        self.assertFalse(self.delivery.state().get('draft_unsupported', False))
        self.assertFalse(self.delivery.state().get('draft_retry_at', 0))
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(self.calls[-1][1]['message_thread_id'], 43)
