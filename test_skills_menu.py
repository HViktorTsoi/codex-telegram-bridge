"""The skills picker lists locally and dispatches one exact native skill."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import RpcError
from topics_bridge import TopicRouter


class SkillsMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {
            'owner_id': '123', 'telegram_state_dir': str(self.path),
            'socket_path': '/unused', 'thread_id': 'meeting-native',
            'expected_bot_username': 'test_bot', 'mode': 'topics', 'host_id': 'A',
            'root_target': 'meeting', 'targets': [
                {'key': 'meeting', 'name': '会议记录', 'thread_id': 'meeting-native', 'message_thread_id': 11},
                {'key': 'instagram', 'name': 'Instagram', 'thread_id': 'instagram-native', 'message_thread_id': 22},
            ],
        }
        (self.path / 'config.json').write_text(json.dumps(self.config))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.sequence = 100
        self.calls, self.native_calls = [], []
        self.cwd = {'meeting-native': '/work/meeting', 'instagram-native': '/work/instagram'}
        self.rows = [
            self.skill('pdf', '/skills/user/pdf/SKILL.md'),
            self.skill('pdf', '/skills/system/pdf/SKILL.md', scope='system'),
            self.skill('disabled-skill', '/skills/disabled/SKILL.md', enabled=False),
        ]
        self.list_error, self.response_cwd, self.native_error = None, None, None
        test = self

        class Native:
            async def call(self, method, params):
                test.native_calls.append((method, copy.deepcopy(params)))
                if test.native_error:
                    raise test.native_error
                if method == 'thread/read':
                    tid = params['threadId']
                    return {'thread': {'id': tid, 'name': tid, 'cwd': test.cwd[tid],
                                       'canAcceptDirectInput': True, 'status': {'type': 'idle'}}}
                if method == 'skills/list':
                    if test.list_error:
                        raise test.list_error
                    cwd = test.response_cwd if test.response_cwd is not None else params['cwds'][0]
                    return {'data': [{'cwd': cwd, 'skills': copy.deepcopy(test.rows), 'errors': []}]}
                if method == 'turn/start':
                    return {'turn': {'id': 'actual-native-turn-' + str(len(test.native_calls))}}
                raise AssertionError('Unexpected native operation: ' + method)

        class Telegram:
            def __init__(self):
                self.next_id, self.messages = 1000, {}

            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method == 'sendMessage':
                    self.next_id += 1
                    result = {**params, 'message_id': self.next_id, 'date': 1,
                              'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 444}}
                    self.messages[self.next_id] = result
                    return copy.deepcopy(result)
                if method == 'editMessageText':
                    result = {**self.messages[params['message_id']], **params}
                    self.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                raise AssertionError('Unexpected Telegram operation: ' + method)

        self.telegram = Telegram()
        self.router.telegram = self.telegram
        for bridge in self.router.bridges.values():
            bridge.codex, bridge.telegram = Native(), self.telegram
            bridge.ready.set()

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def skill(name, path, enabled=True, scope='user'):
        return {'name': name, 'path': path, 'description': name + ' 的用途说明',
                'scope': scope, 'enabled': enabled, 'pluginId': None}

    def message(self, text, topic=11, sender=123, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'date': 1, 'from': {'id': sender},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, **extra}
        if topic is not None:
            message['message_thread_id'] = topic
        return message

    async def send(self, text, topic=11, sender=123, **extra):
        message = self.message(text, topic, sender, **extra)
        update = {'update_id': self.sequence, 'message': message}
        self.router.ingest(update)
        await self.router.skills.handle_one()
        return update

    def last(self):
        return copy.deepcopy(self.telegram.messages[self.telegram.next_id])

    def panel(self):
        for method, params in reversed(self.calls):
            if method in ('sendMessage', 'editMessageText') and params.get('reply_markup', {}).get('inline_keyboard'):
                return copy.deepcopy(self.telegram.messages[params.get('message_id', self.telegram.next_id)])
        raise AssertionError('No skills panel')

    @staticmethod
    def buttons(panel):
        return [button for row in panel.get('reply_markup', {}).get('inline_keyboard', []) for button in row]

    def callback(self, panel, label, sender=123, index=0, **changes):
        matches = [button for button in self.buttons(panel) if label in button['text']]
        self.assertGreater(len(matches), index, (label, self.buttons(panel)))
        self.sequence += 1
        origin = copy.deepcopy(panel)
        origin.update(changes)
        return {'update_id': self.sequence, 'callback_query': {
            'id': str(self.sequence), 'from': {'id': sender}, 'message': origin,
            'data': matches[index]['callback_data'],
        }}

    async def click(self, panel, label, sender=123, index=0, **changes):
        update = self.callback(panel, label, sender, index, **changes)
        self.router.ingest(update)
        await self.router.skills.handle_one()
        return update

    async def prompt(self, topic=11, skill_label='pdf', index=0):
        await self.send('/skills', topic)
        await self.click(self.panel(), skill_label, index=index)
        await self.click(self.panel(), '使用此技能')
        prompt = self.last()
        self.assertTrue(prompt['reply_markup']['force_reply'])
        return prompt

    async def answer(self, prompt, text='只处理这句话\n保留第二行 **原文**', topic=11, sender=123, **extra):
        return await self.send(text, topic, sender, reply_to_message=copy.deepcopy(prompt), **extra)

    def pending(self, key='meeting'):
        return self.router.bridges[key].store.pending()

    def input_count(self, key='meeting'):
        return self.router.bridges[key].store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]

    def starts(self):
        return [params for method, params in self.native_calls if method == 'turn/start']

    def expire(self, table):
        columns = [row[1] for row in self.router.store.db.execute('PRAGMA table_info(' + table + ')')]
        column = next(name for name in columns if name.startswith('expires'))
        with self.router.store.db:
            self.router.store.db.execute('UPDATE ' + table + ' SET ' + column + '=?', (time.time() - 1,))

    async def test_topic_list_uses_native_cwd_and_never_starts_a_model(self):
        await self.send('/skills', 22)
        panel = self.panel()
        self.assertEqual(panel['message_thread_id'], 22)
        self.assertIn('/work/instagram', panel['text'])
        self.assertEqual([params['threadId'] for method, params in self.native_calls if method == 'thread/read'],
                         ['instagram-native'])
        self.assertEqual([params['cwds'] for method, params in self.native_calls if method == 'skills/list'],
                         [['/work/instagram']])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.input_count() + self.input_count('instagram'), 0)

    async def test_all_chooses_recent_local_target_before_any_native_query(self):
        self.router.recent.record('meeting-native', 'old-use', 100)
        self.router.recent.record('instagram-native', 'new-use', 200)
        await self.send('/skills', None)
        chooser = self.panel()
        self.assertNotIn('message_thread_id', chooser)
        self.assertIn('Instagram', self.buttons(chooser)[0]['text'])
        self.assertEqual(self.native_calls, [])
        await self.click(chooser, 'Instagram')
        self.assertIn('/work/instagram', self.panel()['text'])
        self.assertEqual(self.starts(), [])

    async def test_same_name_skills_are_distinct_and_show_their_real_paths(self):
        await self.send('/skills')
        listing = self.panel()
        pdf_buttons = [button for button in self.buttons(listing) if 'pdf' in button['text']]
        self.assertEqual(len(pdf_buttons), 2)
        self.assertNotEqual(pdf_buttons[0]['callback_data'], pdf_buttons[1]['callback_data'])
        await self.click(listing, 'pdf', index=0)
        first = self.panel()['text']
        await self.click(self.panel(), '返回列表')
        await self.click(self.panel(), 'pdf', index=1)
        second = self.panel()['text']
        for path in ('/skills/user/pdf/SKILL.md', '/skills/system/pdf/SKILL.md'):
            self.assertEqual(sum(path in text for text in (first, second)), 1)

    async def test_disabled_skill_can_be_inspected_but_cannot_be_used(self):
        await self.send('/skills')
        await self.click(self.panel(), 'disabled-skill')
        panel = self.panel()
        self.assertFalse(any('使用此技能' in button['text'] for button in self.buttons(panel)))
        self.assertIn('/skills/disabled/SKILL.md', panel['text'])
        self.assertEqual(self.starts(), [])

    async def test_keyword_filter_keeps_distinct_matching_skill_paths(self):
        await self.send('/skills pdf')
        listing = self.panel()
        self.assertEqual(sum('pdf' in button['text'] for button in self.buttons(listing)), 2)
        self.assertFalse(any('disabled-skill' in button['text'] for button in self.buttons(listing)))
        self.assertEqual(self.starts(), [])

    async def test_pages_use_saved_catalog_and_refresh_reloads_the_native_list(self):
        self.rows = [self.skill('skill-' + str(i), '/skills/' + str(i) + '/SKILL.md') for i in range(20)]
        await self.send('/skills')
        calls = len(self.native_calls)
        await self.click(self.panel(), '下一页')
        self.assertEqual(len(self.native_calls), calls)
        await self.click(self.panel(), '刷新')
        self.assertTrue(next(params for method, params in reversed(self.native_calls)
                             if method == 'skills/list')['forceReload'])
        self.assertEqual(self.starts(), [])

    async def test_unknown_topic_never_uses_the_default_session(self):
        await self.send('/skills', 999)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.input_count() + self.input_count('instagram'), 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertIn('绑定', self.last()['text'])

    async def test_unmatched_response_directory_is_not_shown_as_current_skills(self):
        self.response_cwd = '/other-machine/work'
        await self.send('/skills')
        self.assertFalse(any('pdf' in button['text'] for button in self.buttons(self.last())))
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.input_count(), 0)

    async def test_unsupported_native_query_does_not_fall_through_to_a_model(self):
        self.list_error = RpcError('skills/list is not supported')
        await self.send('/skills')
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.input_count(), 0)
        self.assertNotIn('pdf', self.last()['text'])

    async def test_button_requires_exact_owner_bot_topic_message_and_chat(self):
        prompt = await self.prompt()
        self.assertTrue(prompt)
        await self.send('/skills')
        await self.click(self.panel(), 'pdf')
        detail = self.panel()
        for sender, changes in [
            (999, {}), (123, {'from': {'id': 555}}), (123, {'message_thread_id': 22}),
            (123, {'message_id': detail['message_id'] + 100}),
            (123, {'chat': {'id': 999, 'type': 'private'}}),
        ]:
            with self.subTest(sender=sender, changes=changes):
                before = self.telegram.next_id
                await self.click(detail, '使用此技能', sender=sender, **changes)
                self.assertFalse(self.last().get('reply_markup', {}).get('force_reply')
                                 and self.telegram.next_id != before)
                self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.starts(), [])

    async def test_expired_button_cannot_create_a_task_prompt(self):
        await self.send('/skills')
        await self.click(self.panel(), 'pdf')
        detail = self.panel()
        self.expire('skills_buttons')
        await self.click(detail, '使用此技能')
        self.assertFalse(self.last().get('reply_markup', {}).get('force_reply'))
        self.assertEqual(self.input_count(), 0)

    async def test_binding_changed_after_selection_is_rejected(self):
        prompt = await self.prompt()
        self.router.targets['meeting']['message_thread_id'] = 333
        await self.answer(prompt)
        self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.starts(), [])

    async def test_expired_task_reply_is_consumed_without_entering_normal_chat(self):
        prompt = await self.prompt()
        self.expire('skills_prompts')
        await self.answer(prompt, '过期任务不能偷偷变普通模型请求')
        self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.starts(), [])

    async def test_task_reply_in_wrong_topic_or_from_nonowner_is_rejected(self):
        prompt = await self.prompt()
        await self.answer(prompt, topic=22)
        await self.answer(prompt, sender=999)
        self.assertEqual(self.input_count() + self.input_count('instagram'), 0)
        self.assertEqual(self.starts(), [])

    async def test_revoking_owner_access_rejects_an_existing_task_prompt(self):
        prompt = await self.prompt()
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'disabled', 'allowFrom': []}))
        await self.answer(prompt)
        self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.starts(), [])

    async def test_task_dispatch_uses_structured_skill_and_preserves_original_text(self):
        await self.send('/skills')
        await self.click(self.panel(), 'pdf')
        detail = self.panel()
        selected_path = next(skill['path'] for skill in self.rows if skill['path'] in detail['text'])
        await self.click(detail, '使用此技能')
        prompt = self.last()
        original = '请按原样处理这句话\n第二行含 **Markdown** 与 $ 符号。'
        await self.answer(prompt, original)
        row = self.pending()
        self.assertIsNotNone(row)
        self.assertEqual(row['text'], original)
        self.assertIn(row['reply_json'], (None, '', '{}'))
        skill = json.loads(row['skill_json'])
        self.assertEqual(skill, {'name': 'pdf', 'path': selected_path, 'cwd': '/work/meeting'})
        await self.router.bridges['meeting'].dispatch_one(row)
        self.assertEqual(self.starts(), [{
            'threadId': 'meeting-native',
            'input': [{'type': 'text', 'text': original},
                      {'type': 'skill', 'name': 'pdf', 'path': selected_path}],
            'clientUserMessageId': row['client_id'],
        }])
        self.assertTrue(next(params for method, params in reversed(self.native_calls)
                             if method == 'skills/list')['forceReload'])

    async def test_all_task_goes_to_selected_topic_and_echoes_original_text(self):
        await self.send('/skills', None)
        await self.click(self.panel(), 'Instagram')
        await self.click(self.panel(), 'pdf')
        await self.click(self.panel(), '使用此技能')
        prompt = self.last()
        original = 'All 中输入的原始任务\n不要改写。'
        await self.answer(prompt, original, topic=None)
        row = self.pending('instagram')
        self.assertIsNotNone(row)
        self.assertEqual((row['message_thread_id'], row['text']), (22, original))
        self.assertIsNone(self.pending())
        echoes = self.router.bridges['instagram'].store.db.execute(
            "SELECT text,message_thread_id FROM outbox WHERE key LIKE 'main-guard-input:skills:%'").fetchall()
        self.assertEqual(len(echoes), 1)
        self.assertIn(original, echoes[0]['text'])
        self.assertEqual(echoes[0]['message_thread_id'], 22)
        self.assertEqual(self.starts(), [])

    async def test_explicit_skill_prompt_reply_keeps_at_text_out_of_alias_dispatch(self):
        prompt = await self.prompt()
        original = '@instagram 这段是技能任务的正文，不是另一次派单。'
        await self.answer(prompt, original)
        row = self.pending()
        self.assertEqual(row['text'], original)
        self.assertEqual(json.loads(row['skill_json'])['name'], 'pdf')
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        self.assertEqual(self.input_count('instagram'), 0)

    async def test_duplicate_reply_and_restart_never_enqueue_or_start_twice(self):
        prompt = await self.prompt()
        update = await self.answer(prompt, '只执行一次')
        self.router.ingest(update)
        await self.router.skills.handle_one()
        from skills_menu import SkillsMenu
        self.router.skills = SkillsMenu(self.router)
        self.router.skills.recover()
        await self.answer(prompt, '只执行一次')
        self.assertEqual(self.input_count(), 1)
        row = self.pending()
        await self.router.bridges['meeting'].dispatch_one(row)
        self.assertEqual(len(self.starts()), 1)
        self.assertIsNone(self.pending())

    async def test_duplicate_use_button_does_not_create_multiple_live_prompts(self):
        await self.send('/skills')
        await self.click(self.panel(), 'pdf')
        detail = self.panel()
        update = self.callback(detail, '使用此技能')
        self.router.ingest(update)
        await self.router.skills.handle_one()
        first = self.last()
        self.router.ingest(update)
        await self.router.skills.handle_one()
        await self.click(detail, '使用此技能')
        prompts = [params for method, params in self.calls if method == 'sendMessage'
                   and params.get('reply_markup', {}).get('force_reply')]
        self.assertEqual(len(prompts), 1)
        await self.answer(first)
        self.assertEqual(self.input_count(), 1)

    async def interrupted_enqueue(self, after_insert):
        prompt = await self.prompt()
        message = self.message('中断后恢复的一次任务', reply_to_message=copy.deepcopy(prompt))
        update = {'update_id': self.sequence, 'message': message}
        self.router.ingest(update)
        store = self.router.bridges['meeting'].store
        original = store.accept

        def interrupted(*args, **kwargs):
            if after_insert:
                original(*args, **kwargs)
            raise asyncio.CancelledError()

        with patch.object(store, 'accept', side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.router.skills.handle_one()
        self.assertEqual(self.input_count(), int(after_insert))
        from skills_menu import SkillsMenu
        self.router.skills = SkillsMenu(self.router)
        self.router.skills.recover()
        await self.router.skills.handle_one()
        self.assertEqual(self.input_count(), 1)
        await self.router.bridges['meeting'].dispatch_one(self.pending())
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.starts()[0]['input'][0]['text'], '中断后恢复的一次任务')
        self.assertIsNone(self.pending())

    async def test_restart_before_child_accept_recovers_one_task(self):
        await self.interrupted_enqueue(False)

    async def test_restart_after_child_accept_does_not_enqueue_a_second_task(self):
        await self.interrupted_enqueue(True)

    async def test_unrelated_messages_and_system_commands_keep_the_normal_route(self):
        await self.prompt()
        await self.send('普通消息不属于技能向导')
        self.assertTrue(self.router.forward_one())
        row = self.pending()
        self.assertEqual(row['text'], '普通消息不属于技能向导')
        self.assertFalse(row['skill_json'])
        self.assertEqual(self.starts(), [])
        await self.send('/model')
        self.assertTrue(self.router.forward_one())
        rows = self.router.bridges['meeting'].store.db.execute('SELECT text,skill_json FROM inbox ORDER BY id').fetchall()
        self.assertEqual([r['text'] for r in rows], ['普通消息不属于技能向导', '/model'])
        self.assertFalse(rows[-1]['skill_json'])

    async def test_media_caption_is_not_a_skills_command_and_does_not_run_a_model(self):
        await self.send('/skills', document={'file_id': 'doc', 'file_name': 'notes.txt'})
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.router.forward_one())
        self.assertEqual(self.input_count(), 1)
        self.assertFalse(self.pending()['skill_json'])

    async def test_media_and_slash_prompt_replies_do_not_become_skill_tasks(self):
        prompt = await self.prompt()
        await self.answer(prompt, '带附件的任务', document={'file_id': 'doc', 'file_name': 'notes.txt'})
        self.assertEqual(self.input_count(), 0)
        await self.answer(prompt, '/stop')
        self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.starts(), [])

    async def test_cancel_only_cancels_the_skill_prompt(self):
        prompt = await self.prompt()
        await self.answer(prompt, '/cancel')
        await self.answer(prompt, '取消后不应执行')
        self.assertEqual(self.input_count(), 0)
        self.assertEqual(self.starts(), [])

    async def test_dispatch_rejects_removed_disabled_or_moved_skills(self):
        initial = copy.deepcopy(self.rows)
        for change in ('removed', 'disabled', 'cwd'):
            with self.subTest(change=change):
                self.rows, self.cwd['meeting-native'] = copy.deepcopy(initial), '/work/meeting'
                prompt = await self.prompt()
                await self.answer(prompt, '提交前必须重新核对 ' + change)
                row = self.pending()
                self.assertIsNotNone(row)
                selected_path = json.loads(row['skill_json'])['path']
                if change == 'removed':
                    self.rows = [s for s in self.rows if s['path'] != selected_path]
                elif change == 'disabled':
                    next(s for s in self.rows if s['path'] == selected_path)['enabled'] = False
                else:
                    self.cwd['meeting-native'] = '/work/another-project'
                await self.router.bridges['meeting'].dispatch_one(row)
                saved = self.router.bridges['meeting'].store.db.execute('SELECT state FROM inbox WHERE id=?',
                                                                       (row['id'],)).fetchone()
                self.assertIn(saved['state'], ('failed', 'blocked', 'denied'))
                self.assertEqual(self.starts(), [])

    async def test_dispatch_refuses_corrupt_skill_metadata_without_starting(self):
        prompt = await self.prompt()
        await self.answer(prompt)
        store = self.router.bridges['meeting'].store
        with store.db:
            store.db.execute('UPDATE inbox SET skill_json=?', ('{"name":"pdf"}',))
        await self.router.bridges['meeting'].dispatch_one(store.pending())
        self.assertEqual(self.starts(), [])
        self.assertIsNone(store.pending())


if __name__ == '__main__':
    unittest.main()
