import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from recent_sessions import RecentSessions
import test_main_guard as fixtures
import test_session_picker as picker_fixtures


class RecentSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'recent.sqlite'
        self.db = sqlite3.connect(self.path)
        self.now = 1000
        self.recent = RecentSessions(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_recent_user_access_sorts_first_and_ties_preserve_config_order(self):
        targets = [{'thread_id': 'a', 'name': 'A'}, {'thread_id': 'b', 'name': 'B'},
                   {'thread_id': 'c', 'name': 'C'}, {'thread_id': 'd', 'name': 'D'}]
        self.recent.record('b', 'message:1', 100)
        self.recent.record('c', 'message:2', 200)
        self.recent.record('d', 'message:3', 200)
        ordered = self.recent.ordered(targets)
        self.assertEqual([t['thread_id'] for t in ordered], ['c', 'd', 'b', 'a'])
        self.assertEqual([t['thread_id'] for t in targets], ['a', 'b', 'c', 'd'])
        self.assertIs(ordered[0], targets[2])

    def test_duplicate_event_and_late_older_event_never_bump_or_reverse_time(self):
        self.assertTrue(self.recent.record('a', 'message:1', 200))
        self.now = 5000
        self.assertFalse(self.recent.record('a', 'message:1'))
        self.assertEqual(self.recent.last('a'), 200)
        self.assertTrue(self.recent.record('a', 'message:old', 100))
        self.assertEqual(self.recent.last('a'), 200)
        self.assertFalse(self.recent.record('other', 'message:1', 6000))
        self.assertEqual(self.recent.last('other'), 0)

    def test_restart_preserves_time_and_bootstrap_seed_is_once_per_thread(self):
        self.recent.record('a', 'bootstrap:a', 100)
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.recent = RecentSessions(self.db, clock=lambda: 10000)
        self.assertFalse(self.recent.record('a', 'bootstrap:a', 9000))
        self.assertEqual(self.recent.last('a'), 100)
        self.recent.record('a', 'telegram:1', 300)
        self.assertEqual(self.recent.times(), {'a': 300})

    def test_native_updated_times_and_names_do_not_reorder_without_user_access(self):
        targets = [{'thread_id': 'a', 'name': 'Z', 'updatedAt': 100000},
                   {'thread_id': 'b', 'name': 'A', 'updatedAt': 1}]
        self.recent.record('b', 'telegram:1', 200)
        self.recent.record('a', 'telegram:2', 100)
        self.assertEqual(self.recent.ordered(targets)[0]['thread_id'], 'b')
        targets[0].update(name='New A', updatedAt=1000000)
        self.assertEqual(self.recent.ordered(targets)[0]['thread_id'], 'b')

    def test_invalid_time_cannot_poison_sort_order(self):
        for stamp in [float('nan'), float('inf'), -1, 'not a timestamp']:
            self.assertFalse(self.recent.record('a', 'invalid:' + str(stamp), stamp))
        self.assertEqual(self.recent.times(), {})
        self.assertFalse(self.recent.record('', 'empty', 100))
        self.assertFalse(self.recent.record('a', '', 100))


class CatalogRecentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = picker_fixtures.PickerTests.asyncSetUp
    asyncTearDown = picker_fixtures.PickerTests.asyncTearDown
    menu = picker_fixtures.PickerTests.menu
    click = picker_fixtures.PickerTests.click
    callback = picker_fixtures.PickerTests.callback

    async def test_complete_catalog_orders_directories_and_sessions_but_snapshot_stays_stable(self):
        pages = [
            [{'id': 'a-new', 'name': 'A newer', 'cwd': '/work/a'},
             {'id': 'a-old', 'name': 'A used', 'cwd': '/work/a'},
             {'id': 'a-other', 'name': 'A other', 'cwd': '/work/a'},
             {'id': 'unknown-first', 'name': 'C untouched', 'cwd': '/work/c'}],
            [{'id': 'b-old', 'name': 'B older use', 'cwd': '/work/b'},
             {'id': 'b-new', 'name': 'B latest use', 'cwd': '/work/b'},
             {'id': 'unknown-second', 'name': 'D untouched', 'cwd': '/work/d'}],
        ]

        async def metadata(method, params):
            if method == 'thread/list':
                return {'data': pages[1]} if params.get('cursor') else {'data': pages[0], 'nextCursor': 'later'}
            if method == 'thread/read':
                return {'thread': {'id': params['threadId'], 'cwd': '/work'}}
            raise AssertionError(method)

        self.router.catalog_call = AsyncMock(side_effect=metadata)
        self.router.recent.record('b-new', 'latest', 300)
        self.router.recent.record('a-old', 'second', 200)
        self.router.recent.record('b-old', 'older', 100)
        menu = await self.menu()
        directories = await self.click(menu, '添加会话')
        snapshot = json.loads(self.router.store.db.execute('SELECT payload FROM ui_catalogs').fetchone()[0])
        self.assertEqual([g['cwd'] for g in snapshot], ['/work/b', '/work/a', '/work/c', '/work/d'])
        self.assertEqual([t['id'] for t in snapshot[0]['threads']], ['b-new', 'b-old'])
        self.assertEqual([t['id'] for t in snapshot[1]['threads']], ['a-old', 'a-new', 'a-other'])
        selected = await self.click(directories, '1. 📁 ')
        self.assertIn('/work/b', selected['text'])
        self.assertIn('最近使用在前', selected['text'])
        self.assertIn('B latest use', selected['reply_markup']['inline_keyboard'][0][0]['text'])
        self.router.recent.record('a-new', 'new-access', 500)
        # Other user activity does not rearrange the currently browsed snapshot.
        other = await self.click(selected, '下个目录')
        self.assertIn('/work/a', other['text'])
        self.assertIn('A used', other['reply_markup']['inline_keyboard'][0][0]['text'])
        directory_index = await self.click(other, '返回目录列表')
        self.assertIn('/work/b', directory_index['reply_markup']['inline_keyboard'][0][0]['text'])
        self.assertEqual(sum(c.args[0] == 'thread/list' for c in self.router.catalog_call.call_args_list), 2)
        # An explicit reload uses the newer access record while preserving all IDs.
        reloaded = await self.click(directory_index, '重新加载')
        self.assertIn('/work/a', reloaded['reply_markup']['inline_keyboard'][0][0]['text'])
        selected = await self.click(reloaded, '1. 📁 ')
        self.assertIn('A newer', selected['reply_markup']['inline_keyboard'][0][0]['text'])
        self.assertEqual(sum(c.args[0] == 'thread/list' for c in self.router.catalog_call.call_args_list), 4)
        self.assertEqual(self.telegram.created, [])
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))


class MainGuardRecentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MainGuardTests.asyncSetUp
    asyncTearDown = fixtures.MainGuardTests.asyncTearDown
    message = fixtures.MainGuardTests.message
    send = fixtures.MainGuardTests.send
    pump = fixtures.MainGuardTests.pump
    click = fixtures.MainGuardTests.click
    callback = fixtures.MainGuardTests.callback
    inbox = fixtures.MainGuardTests.inbox

    def choices(self, panel):
        targets = []
        for row in panel['reply_markup']['inline_keyboard']:
            for button in row:
                saved = self.router.store.db.execute('SELECT action,data FROM ui_buttons WHERE token=?',
                                                     (button['callback_data'][3:],)).fetchone()
                if saved['action'] == 'main:select':
                    import json
                    targets.append(json.loads(saved['data'])['target']['thread_id'])
        return targets

    async def test_main_selector_uses_access_order_and_cancelled_selection_does_not_change_it(self):
        self.router.recent.record('meeting-native', 'seed:meeting', 200)
        self.router.recent.record('instagram-native', 'seed:instagram', 100)
        _, panel = await self.send('先不要发送')
        self.assertEqual(self.choices(panel), ['meeting-native', 'instagram-native'])
        review = await self.click(panel, 'Instagram')
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        await self.click(review, '取消')
        _, next_panel = await self.send('仍按真正访问顺序')
        self.assertEqual(self.choices(next_panel), ['meeting-native', 'instagram-native'])

    async def test_confirmed_handoff_moves_session_to_top_and_recovery_does_not_bump_it(self):
        self.router.recent.record('meeting-native', 'seed:meeting', self.now - 20)
        _, panel = await self.send('确认后才算访问')
        review = await self.click(panel, 'Instagram')
        self.assertEqual(self.router.recent.last('instagram-native'), 0)
        confirmed = self.now
        await self.click(review, '确认发送')
        self.assertEqual(self.router.recent.last('instagram-native'), confirmed)
        self.now += 100
        self.router.picker.guard.recover()
        self.assertEqual(self.router.recent.last('instagram-native'), confirmed)
        _, panel = await self.send('最近使用 Instagram')
        self.assertEqual(self.choices(panel)[0], 'instagram-native')

    async def test_recording_failure_after_child_commit_recovers_original_confirmation_time_once(self):
        _, panel = await self.send('恢复同一次确认')
        review = await self.click(panel, 'Instagram')
        confirmed = self.now
        with patch.object(self.router.recent, 'record', side_effect=OSError('interrupted metadata commit')):
            await self.click(review, '确认发送')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.router.recent.last('instagram-native'), 0)
        self.now += 100
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.router.recent.last('instagram-native'), confirmed)

    async def test_bot_output_and_failed_confirmation_do_not_make_a_session_recent(self):
        self.router.recent.record('meeting-native', 'seed:meeting', 200)
        self.router.recent.record('instagram-native', 'seed:instagram', 100)
        self.router.bridges['instagram'].store.enqueue_at('turn:bot:reply', '123', '后台回复', 22)
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        _, panel = await self.send('旧目标不能确认')
        review = await self.click(panel, 'Instagram')
        self.router.targets['instagram']['message_thread_id'] = 99
        await self.click(review, '确认发送')
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        self.assertEqual(self.inbox(), [])

    async def test_thread_handoff_uses_durable_receipt_and_replay_cannot_make_it_newer(self):
        with patch('topics_bridge.now_ms', return_value=100000):
            update = self.message('真正访问话题', topic=22)
            self.router.ingest(update)
        with patch.object(self.router.store, 'finish', side_effect=OSError('interrupted acknowledgement')):
            with self.assertRaises(OSError):
                self.router.forward_one()
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        with patch('topics_bridge.now_ms', return_value=900000):
            self.assertTrue(self.router.forward_one())
            self.router.ingest(update)
            self.assertFalse(self.router.forward_one())
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        self.assertEqual(len(self.inbox()), 1)

    async def test_global_and_unknown_commands_do_not_reorder_but_session_status_does(self):
        self.router.recent.record('meeting-native', 'seed:meeting', 200)
        for command in ['/help', '/unrecognized']:
            with patch('topics_bridge.now_ms', return_value=900000):
                self.router.ingest(self.message(command, topic=11))
                self.assertTrue(self.router.forward_one())
            self.assertEqual(self.router.recent.last('meeting-native'), 200)
        with patch('topics_bridge.now_ms', return_value=1000000):
            self.router.ingest(self.message('/status', topic=11))
            self.assertTrue(self.router.forward_one())
        self.assertEqual(self.router.recent.last('meeting-native'), 1000)

    async def test_explicit_dispatch_records_original_request_time_once(self):
        update = self.message('@instagram 明确派单')
        with patch('control_center.time.time', return_value=700):
            self.router.ingest(update)
            self.assertTrue(await self.router.control.handle_one())
            await self.router.control.synchronize()
        self.assertEqual(self.router.recent.last('instagram-native'), 0)
        self.assertTrue(self.router.control.deliver_one())
        self.assertEqual(self.router.recent.last('instagram-native'), 700)
        with self.router.store.db:
            self.router.store.db.execute("UPDATE cc_deliveries SET state='importing'")
        with patch('control_center.time.time', return_value=900):
            self.assertTrue(self.router.control.deliver_one())
        self.assertEqual(self.router.recent.last('instagram-native'), 700)
        self.assertEqual(len(self.inbox()), 1)

    async def test_bootstrap_uses_accepted_inputs_only_and_runs_once_per_native_id(self):
        store = self.router.bridges['instagram'].store
        for uid, stamp, state in [(1, 100000, 'done'), (2, 900000, 'denied'), (3, 950000, 'failed')]:
            store.accept({'update_id': uid}, {'chat': {'id': 123}, 'text': 'legacy input', 'message_thread_id': 22})
            with store.db:
                store.db.execute('UPDATE inbox SET received_ms=?,state=? WHERE id=?', (stamp, state, uid))
        with self.router.store.db:
            self.router.store.db.execute('DELETE FROM session_access_events WHERE event_id=?',
                                         ('bootstrap:instagram-native',))
            self.router.store.db.execute('DELETE FROM session_recent_access WHERE thread_id=?', ('instagram-native',))
        self.router.recent.bootstrap(self.router.bridges.values())
        self.assertEqual(self.router.recent.last('instagram-native'), 100)
        with store.db:
            store.db.execute('UPDATE inbox SET received_ms=1000000 WHERE id=1')
        self.router.recent.bootstrap(self.router.bridges.values())
        self.assertEqual(self.router.recent.last('instagram-native'), 100)


if __name__ == '__main__':
    unittest.main()
