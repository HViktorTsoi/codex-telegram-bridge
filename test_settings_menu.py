"""Button settings stay local and never invoke a model turn."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from settings_menu import SettingsMenu
from topics_bridge import TopicRouter


class SettingsMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        config = {'owner_id': '123', 'telegram_state_dir': str(self.path), 'socket_path': '/unused',
                  'thread_id': 'manager-native', 'root_target': 'manager', 'mode': 'topics',
                  'expected_bot_username': 'test_bot', 'host_id': 'local', 'targets': [
                      {'key': 'manager', 'name': 'Manager', 'thread_id': 'manager-native', 'message_thread_id': 42},
                      {'key': 'other', 'name': 'Other', 'thread_id': 'other-native', 'message_thread_id': 99}]}
        (self.path / 'config.json').write_text(json.dumps(config))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.router = TopicRouter(config, self.path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.ui = getattr(self.router, 'settings_menu', None) or SettingsMenu(self.router)
        self.sequence, self.calls, self.updates, self.native_reads = 100, [], [], []
        self.messages, self.settings, self.fail_update = {}, {}, None
        self.after_models, self.block_models = None, None
        self.catalog = [self.model('native-A', ['low', 'high'], 'low'),
                        self.model('native-B', ['Medium', 'XHigh'], 'Medium')]
        test = self

        class Native:
            def __init__(self, tid):
                self.tid = tid

            async def settings(self):
                test.native_reads.append(('settings', self.tid))
                return copy.deepcopy(test.settings[self.tid])

            async def models(self):
                test.native_reads.append(('models', self.tid))
                if test.block_models and self.tid == 'manager-native':
                    await test.block_models.wait()
                if test.after_models:
                    await test.after_models()
                return copy.deepcopy(test.catalog)

            async def call(self, method, params):
                if method != 'thread/settings/update':
                    raise AssertionError('No turn, permission or other operation is allowed: ' + method)
                test.updates.append((self.tid, copy.deepcopy(params)))
                if test.fail_update:
                    raise test.fail_update
                for key, field in [('model', 'model'), ('effort', 'reasoningEffort')]:
                    if key in params:
                        test.settings[self.tid][field] = params[key]
                return {}

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method == 'sendMessage':
                    test.sequence += 1
                    result = {**params, 'message_id': test.sequence, 'date': 1,
                              'from': {'id': 444}, 'chat': {'id': 123, 'type': 'private'}}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                if method == 'editMessageText':
                    result = {**test.messages[params['message_id']], **params}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                raise AssertionError(method)

        self.router.telegram = Telegram()
        for bridge in self.router.bridges.values():
            tid = bridge.thread_id
            self.settings[tid] = {'thread': {'id': tid}, 'model': 'native-A', 'reasoningEffort': 'high'}
            bridge.codex, bridge.telegram = Native(tid), self.router.telegram
            bridge.ready.set()

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def model(name, efforts, default):
        return {'id': name, 'model': name, 'supportedReasoningEfforts':
                [{'reasoningEffort': value} for value in efforts], 'defaultReasoningEffort': default}

    def message(self, text, topic=42, sender=123, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'date': 1, 'chat': {'id': 123, 'type': 'private'},
                   'from': {'id': sender}, 'text': text, **extra}
        if topic is not None:
            message['message_thread_id'] = topic
        return {'update_id': self.sequence, 'message': message}

    async def send(self, text, topic=42, **extra):
        update = self.message(text, topic, **extra)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return self.panel()

    def panel(self):
        for method, params in reversed(self.calls):
            if method in ('sendMessage', 'editMessageText') and params.get('reply_markup', {}).get('inline_keyboard'):
                mid = params.get('message_id') or max(self.messages)
                return copy.deepcopy(self.messages[mid])
        raise AssertionError('No settings panel')

    def callback(self, panel, text, sender=123, **changes):
        button = next(button for row in panel['reply_markup']['inline_keyboard'] for button in row if text in button['text'])
        self.sequence += 1
        return {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence),
                'from': {'id': sender}, 'data': button['callback_data'], 'message': {**copy.deepcopy(panel), **changes}}}

    async def click(self, panel, text, **changes):
        update = self.callback(panel, text, **changes)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return update

    @staticmethod
    def labels(panel):
        return [button['text'] for row in panel['reply_markup']['inline_keyboard'] for button in row]

    async def test_model_panel_is_live_topic_local_and_does_not_invoke_a_model(self):
        panel = await self.send('/model', topic=99)
        self.assertEqual(panel['message_thread_id'], 99)
        self.assertIn('Other', panel['text'])
        self.assertIn('✓ native-A', self.labels(panel))
        self.assertIn('native-B', self.labels(panel))
        self.assertEqual(self.updates, [])
        self.assertTrue(all(tid == 'other-native' for _, tid in self.native_reads))
        self.assertTrue(all(len(button['callback_data']) < 64 for row in panel['reply_markup']['inline_keyboard']
                            for button in row if 'callback_data' in button))

    async def test_all_operates_explicit_root_and_keeps_menu_in_all(self):
        panel = await self.send('/effort', topic=None)
        self.assertNotIn('message_thread_id', panel)
        self.assertIn('Manager', panel['text'])
        await self.click(panel, 'low')
        self.assertEqual(self.updates, [('manager-native', {'threadId': 'manager-native', 'effort': 'low'})])
        self.assertNotIn('message_thread_id', self.panel())

    async def test_model_change_uses_native_default_when_previous_effort_is_incompatible(self):
        panel = await self.send('/model', topic=99)
        await self.click(panel, 'native-B')
        self.assertEqual(self.updates, [('other-native', {'threadId': 'other-native', 'model': 'native-B', 'effort': 'Medium'})])
        self.assertIn('✓ native-B', self.labels(self.panel()))
        self.assertIn('当前 effort：Medium', self.panel()['text'])
        self.assertEqual(self.settings['manager-native']['model'], 'native-A')

    async def test_effort_values_keep_canonical_case_and_follow_current_model(self):
        self.settings['manager-native'].update(model='native-B', reasoningEffort='Medium')
        panel = await self.send('/effort')
        self.assertIn('✓ Medium', self.labels(panel))
        self.assertNotIn('low', self.labels(panel))
        await self.click(panel, 'XHigh')
        self.assertEqual(self.updates[-1][1], {'threadId': 'manager-native', 'effort': 'XHigh'})

    async def test_old_effort_menu_refreshes_when_model_changed_externally(self):
        panel = await self.send('/effort')
        self.settings['manager-native'].update(model='native-B', reasoningEffort='Medium')
        await self.click(panel, 'low')
        self.assertEqual(self.updates, [])
        self.assertIn('模型已变化', self.panel()['text'])
        self.assertIn('XHigh', self.labels(self.panel()))
        self.assertNotIn('low', self.labels(self.panel()))

    async def test_duplicate_update_and_second_click_do_not_reapply_old_setting(self):
        panel = await self.send('/effort')
        update = await self.click(panel, 'low')
        self.ui.ingest(update)
        await self.ui.handle_one()
        await self.click(panel, 'low')
        self.assertEqual(len(self.updates), 1)
        self.assertEqual(self.settings['manager-native']['reasoningEffort'], 'low')

    async def test_format_menu_works_offline_without_native_lock_and_preserves_text_shortcuts(self):
        bridge = self.router.bridges['manager']
        bridge.ready.clear()
        await bridge.dispatch_lock.acquire()
        try:
            panel = await self.send('/format')
            await asyncio.wait_for(self.click(panel, '纯文本'), 1)
        finally:
            bridge.dispatch_lock.release()
        self.assertEqual(bridge.store.get('output_format'), 'plain')
        self.assertEqual(self.native_reads, [])
        self.assertEqual(self.updates, [])
        for text in ['/model native-B', '/effort high', '/format rich', '/status', '普通输入']:
            self.assertFalse(self.ui.ingest(self.message(text)), text)
        self.assertFalse(self.ui.ingest(self.message('/model', document={'file_id': 'file'})))

    async def test_unauthorized_callback_wrong_topic_and_forged_bot_never_update(self):
        panel = await self.send('/effort')
        await self.click(panel, 'low', sender=999)
        await self.click(panel, 'low', message_thread_id=99)
        await self.click(panel, 'low', **{'from': {'id': 999}})
        self.assertEqual(self.updates, [])

    async def test_rebind_during_catalog_read_blocks_setting_write(self):
        panel = await self.send('/model')

        async def replace():
            self.router.targets['manager'] = {**self.router.targets['manager'], 'message_thread_id': 777}

        self.after_models = replace
        await self.click(panel, 'native-B')
        self.assertEqual(self.updates, [])

    async def test_expired_or_hidden_model_and_unbound_topic_do_not_change_settings(self):
        panel = await self.send('/model')
        self.catalog[1]['hidden'] = True
        await self.click(panel, 'native-B')
        self.assertEqual(self.updates, [])
        self.assertNotIn('native-B', self.labels(self.panel()))
        with self.ui.db:
            self.ui.db.execute('UPDATE settings_panels SET expires_at=0')
        await self.click(self.panel(), 'native-A')
        update = self.message('/model', topic=888)
        self.ui.ingest(update)
        await self.ui.handle_one()
        self.assertEqual(self.updates, [])
        self.assertIn('尚未绑定', self.calls[-1][1]['text'])

    async def test_unknown_native_result_and_recovery_do_not_repeat_mutation(self):
        panel = await self.send('/effort')
        self.fail_update = ConnectionError('unknown native result')
        await self.click(panel, 'low')
        self.assertEqual(self.ui.db.execute('SELECT state FROM settings_applies').fetchone()[0], 'uncertain')
        self.ui.recover()
        await self.click(panel, 'low')
        self.assertEqual(len(self.updates), 1)
        self.fail_update = None
        await self.click(panel, '刷新')
        self.assertIn('✓ high', self.labels(self.panel()))

    async def test_pagination_tabs_and_close_stay_on_one_message(self):
        self.catalog = [self.model('model-' + str(index), ['custom'], 'custom') for index in range(12)]
        panel = await self.send('/model')
        mid = panel['message_id']
        await self.click(panel, '下一页')
        self.assertIn('model-11', self.labels(self.panel()))
        self.assertNotIn('model-0', self.labels(self.panel()))
        await self.click(self.panel(), '格式')
        self.assertIn('Rich Markdown', self.panel()['text'])
        self.assertEqual(self.panel()['message_id'], mid)
        await self.click(self.panel(), '关闭')
        self.assertEqual(self.messages[mid]['reply_markup'], {'inline_keyboard': []})
        self.assertEqual(self.updates, [])

    async def test_current_navigation_tab_is_disabled_in_each_settings_page(self):
        panel = await self.send('/model')
        for current, next_tab in [('模型', '强度'), ('强度', '格式'), ('格式', '模型')]:
            buttons = {button['text']: button for row in panel['reply_markup']['inline_keyboard']
                       for button in row if button['text'] in ('模型', '强度', '格式')}
            self.assertEqual(buttons[current], {'text': current, 'disabled': {}})
            self.assertTrue(all('callback_data' in button and 'disabled' not in button
                                for title, button in buttons.items() if title != current))
            await self.click(panel, next_tab)
            panel = self.panel()
        self.assertEqual(self.updates, [])

    async def test_legacy_current_tab_callback_does_not_reload_or_reset_the_current_page(self):
        self.catalog = [self.model('model-' + str(index), ['custom'], 'custom') for index in range(12)]
        panel = await self.send('/model')
        await self.click(panel, '下一页')
        panel = self.panel()
        stored = self.ui.db.execute('SELECT * FROM settings_panels ORDER BY rowid DESC LIMIT 1').fetchone()
        token = 'legacy-current-model-tab'
        with self.ui.db:
            self.ui.db.execute('INSERT INTO settings_buttons(token,panel_id,revision,action,value,state) '
                               'VALUES(?,?,?,?,?,?)', (token, stored['id'], stored['revision'], 'tab',
                                                      json.dumps('model'), 'active'))
        update = self.callback(panel, '刷新')
        update['callback_query']['data'] = 'sm:' + token
        reads = len(self.native_reads)
        edits = sum(method == 'editMessageText' for method, _ in self.calls)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        actual = self.ui._panel_row(stored['id'])
        self.assertEqual(actual['page'], 1)
        self.assertEqual(actual['revision'], stored['revision'])
        self.assertEqual(len(self.native_reads), reads)
        self.assertEqual(sum(method == 'editMessageText' for method, _ in self.calls), edits)
        self.assertEqual(self.updates, [])

    async def test_native_identity_mismatch_is_rejected_without_a_settings_write(self):
        self.settings['manager-native']['thread']['id'] = 'different-native'
        update = self.message('/model')
        self.ui.ingest(update)
        await self.ui.handle_one()
        self.assertEqual(self.updates, [])
        self.assertIn('身份', self.calls[-1][1]['text'])

    async def test_worker_loop_does_not_block_another_topic_behind_slow_native_query(self):
        self.block_models = asyncio.Event()
        self.ui.ingest(self.message('/model', topic=42))
        self.ui.ingest(self.message('/model', topic=99))
        task = asyncio.create_task(self.ui.loop())
        try:
            for _ in range(40):
                if any(message.get('message_thread_id') == 99 for message in self.messages.values()):
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('Another topic was blocked by a slow native query')
            self.assertTrue(any(message.get('message_thread_id') == 99 for message in self.messages.values()))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
