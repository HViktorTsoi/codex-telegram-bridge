"""Selection commits one durable send intent without a second confirmation."""
import json
import unittest
from unittest.mock import patch

from session_picker import BindingError
import test_main_guard as harness


class MainSelectSendTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.MainGuardTests.asyncSetUp
    asyncTearDown = harness.MainGuardTests.asyncTearDown
    message = harness.MainGuardTests.message
    send = harness.MainGuardTests.send
    pump = harness.MainGuardTests.pump
    click = harness.MainGuardTests.click
    callback = harness.MainGuardTests.callback
    inbox = harness.MainGuardTests.inbox

    async def test_selection_persists_frozen_intent_before_import_and_recovers_once(self):
        update, panel = await self.send('选中后立即发送，且只发送一次')
        guard = self.router.picker.guard
        target = guard._binding(self.router.targets['instagram'])
        seen = []

        def interrupted(draft):
            seen.append(dict(draft))
            raise OSError('Interrupted immediately after the durable selection')

        with patch.object(guard, '_release', side_effect=interrupted):
            await self.click(panel, 'Instagram')
        self.assertEqual(len(seen), 1)
        intent = seen[0]
        self.assertEqual(intent['state'], 'releasing')
        self.assertEqual(json.loads(intent['selection_json']), target)
        self.assertEqual(intent['selected_revision'], intent['revision'])
        self.assertEqual(intent['confirmed_at'], self.now)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        guard.recover()
        guard.recover()
        self.assertEqual([(r['id'], r['text'], r['message_thread_id']) for r in self.inbox()],
                         [(update['update_id'], '选中后立即发送，且只发送一次', 22)])
        self.assertEqual(guard._draft(intent['id'])['state'], 'queued')
        copies = self.router.bridges['instagram'].store.db.execute(
            "SELECT * FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchall()
        self.assertEqual(len(copies), 1)
        self.assertEqual(self.native_calls, [])

    async def test_fresh_album_selection_waits_for_last_part_before_any_import(self):
        await self.send('', caption='相册第一部分', media_group_id='album', photo=[{'file_id': 'one'}])
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        second = self.message('', media_group_id='album', photo=[{'file_id': 'two'}])
        self.router.ingest(second)
        guard = self.router.picker.guard
        draft = guard._draft(self.router.store.db.execute('SELECT id FROM main_guard_drafts').fetchone()[0])
        # Reopening the selector while the album is still arriving can show the
        # newest revision; this still must not bypass the quiet-period check.
        await guard._selector({'callback_query': {'message': panel}}, draft)
        selector = self.telegram.messages[panel['message_id']]
        callback = self.callback(selector, 'Instagram')
        button = self.router.store.db.execute('SELECT data FROM ui_buttons WHERE token=?',
                                               (callback['callback_query']['data'][3:],)).fetchone()
        data = json.loads(button['data'])
        self.assertEqual(data['revision'], draft['revision'])
        with self.assertRaises(BindingError):
            await guard.handle(callback, 'main:select', data)
        self.assertEqual(self.inbox(), [])
        current = guard._draft(draft['id'])
        self.assertEqual(current['state'], 'held')
        self.assertIsNone(current['selection_json'])
        self.now += 2
        await self.click(selector, 'Instagram')
        rows = self.inbox()
        self.assertEqual(len(rows), 2)
        self.assertEqual([json.loads(r['message_json'])['photo'][0]['file_id'] for r in rows], ['one', 'two'])
        self.assertEqual(guard._draft(draft['id'])['state'], 'queued')

    async def test_old_two_step_selection_refreshes_without_sending_after_upgrade(self):
        _, panel = await self.send('旧版面板选择还不代表发送')
        guard = self.router.picker.guard
        draft = self.router.store.db.execute('SELECT * FROM main_guard_drafts').fetchone()
        selected = guard._binding(self.router.targets['instagram'])
        guard._save(draft['id'], state='review', selection_json=json.dumps(selected),
                    selected_revision=draft['revision'])
        old_data = {'id': draft['id'], 'revision': draft['revision'], 'target': selected}
        # A recovered UI job can contain the old main:select payload even
        # though the upgrade no longer generates its confirmation panel.
        await guard.handle({'callback_query': {'message': panel}}, 'main:select', old_data)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(guard._draft(draft['id'])['state'], 'held')
        refreshed = self.telegram.messages[panel['message_id']]
        self.assertIn('点击本机会话即发送', refreshed['text'])
        await self.click(refreshed, 'Instagram')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(guard._draft(draft['id'])['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
