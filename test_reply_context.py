import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from attachments import inputs_for, payload_from
from bridge import Bridge
from reply_context import MAX_QUOTE, MAX_TEXT, snapshot


class SnapshotTests(unittest.TestCase):
    def test_unquoted_and_implicit_topic_root_messages_add_no_context(self):
        for message in ({'text': '普通文字'}, {'text': '/model'}, {'photo': [{'file_id': 'x'}]},
                        {'text': '普通文字', 'message_thread_id': 42,
                         'reply_to_message': {'message_id': 42, 'forum_topic_created': {'name': 'topic'}}}):
            self.assertIsNone(snapshot(message))
        self.assertEqual(payload_from({'text': '/status', 'reply_to_message': {'text': 'quoted'}}), {})

    def test_parent_selected_quote_and_ids_are_kept_without_nested_reply_chain(self):
        result = snapshot({'chat': {'id': 123}, 'reply_to_message': {
            'message_id': 50, 'message_thread_id': 42, 'text': '🙂第一段，第二段',
            'reply_to_message': {'text': '不要递归这段'}},
            'quote': {'text': '第二段', 'position': 7, 'is_manual': True}})
        self.assertEqual(result['text'], '🙂第一段，第二段')
        self.assertEqual((result['chat_id'], result['reply_to_message_id'], result['reply_to_thread_id']), (123, 50, 42))
        self.assertEqual((result['quote'], result['quote_position_utf16']), ('第二段', 7))
        self.assertNotIn('不要递归', json.dumps(result, ensure_ascii=False))

    def test_rich_paragraph_table_and_code_are_readable_without_media_urls(self):
        rich = {'blocks': [
            {'type': 'paragraph', 'text': ['第一段', {'type': 'bold', 'text': '重点'}]},
            {'type': 'table', 'cells': [[{'text': '人数', 'is_header': True}, {'text': '18'}]]},
            {'type': 'pre', 'text': 'print(1)', 'language': 'python'},
            {'type': 'photo', 'url': 'https://private.invalid/photo', 'caption': {'text': '图说明'}}]}
        result = snapshot({'reply_to_message': {'message_id': 50, 'rich_message': rich}})
        self.assertEqual(result['text'], '第一段重点\n人数 | 18\nprint(1)\n图说明')
        self.assertNotIn('private.invalid', json.dumps(result))

    def test_missing_parent_and_external_quote_do_not_invent_original_content(self):
        result = snapshot({'external_reply': {'message_id': 80, 'chat': {'id': -123},
                                              'origin': {'type': 'hidden_user', 'sender_user_name': 'private'}},
                           'quote': {'text': '只有这个片段', 'position': 0}})
        self.assertEqual(result['source'], 'external_reply')
        self.assertEqual(result['reply_to_message_id'], 80)
        self.assertEqual(result['quote'], '只有这个片段')
        self.assertTrue(result['content_unavailable'])
        self.assertNotIn('private', json.dumps(result))

    def test_quoted_media_is_metadata_only_and_long_context_is_explicitly_truncated(self):
        result = snapshot({'reply_to_message': {'message_id': 50, 'caption': '文件说明',
            'document': {'file_id': 'secret-file-id', 'file_name': 'report.pdf'}},
            'quote': {'text': 'x' * (MAX_QUOTE + 10)}})
        self.assertEqual(result['attachments'], [{'kind': 'document', 'file_name': 'report.pdf'}])
        self.assertNotIn('secret-file-id', json.dumps(result))
        self.assertEqual(len(result['quote']), MAX_QUOTE)
        self.assertTrue(result['quote_truncated'])
        result = snapshot({'reply_to_message': {'message_id': 1, 'text': 'x' * (MAX_TEXT + 10)}})
        self.assertEqual(len(result['text']), MAX_TEXT)
        self.assertTrue(result['text_truncated'])


class ReplyInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'owner_id': '123', 'thread_id': 'original-session',
                       'telegram_state_dir': str(self.path), 'socket_path': '/unused'}
        self.bridge = Bridge(self.config, self.path)
        self.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'native-turn'}}),
            'settings': AsyncMock(return_value={'thread': {'id': 'original-session', 'status': {'type': 'idle'}}}),
            'models': AsyncMock(return_value=[])})()
        self.bridge.codex = self.codex

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def accept(self, text, i=1, **extra):
        self.bridge.store.accept({'update_id': i}, {'chat': {'id': 123}, 'text': text, **extra})

    async def test_normal_text_is_exactly_the_original_input_without_extra_segments(self):
        self.accept('原消息\n[原样保留]')
        await self.bridge.dispatch_one(self.bridge.store.pending())
        self.assertEqual(self.codex.call.call_args.args, ('turn/start', {
            'threadId': 'original-session', 'input': [{'type': 'text', 'text': '原消息\n[原样保留]'}],
            'clientUserMessageId': 'telegram-1'}))

    async def test_reply_snapshot_survives_restart_and_does_not_replace_new_request(self):
        self.accept('改一下这里', reply_to_message={'message_id': 50, 'text': '/stop 是被引用文字'},
                    quote={'text': '被引用文字', 'position': 9})
        self.bridge.store.db.close()
        self.bridge = Bridge(self.config, self.path)
        self.bridge.codex = self.codex
        await self.bridge.dispatch_one(self.bridge.store.pending())
        method, params = self.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'original-session'))
        self.assertEqual(params['input'][0], {'type': 'text', 'text': '改一下这里'})
        self.assertEqual(len(params['input']), 2)
        context = json.loads(params['input'][1]['text'].split('\n', 1)[1])
        self.assertEqual((context['reply_to_message_id'], context['text']), (50, '/stop 是被引用文字'))
        self.assertEqual(context['quote'], '被引用文字')

    async def test_reply_metadata_does_not_turn_commands_into_model_tasks(self):
        for i, command in enumerate(['/model', '/effort', '/status', '/format plain', '/help'], 1):
            self.accept(command, i, reply_to_message={'message_id': 50, 'text': '引用文字'})
            await self.bridge.dispatch_one(self.bridge.store.pending())
        self.codex.call.assert_not_called()
        self.assertEqual(self.bridge.store.get('output_format'), 'plain')

    async def test_new_photo_with_quote_keeps_visual_input_and_never_downloads_quoted_photo(self):
        photo = self.path / 'photo.png'
        photo.write_bytes(b'\x89PNG\r\n\x1a\n')
        self.accept('', caption='/status 是图片说明', photo=[{'file_id': 'new-photo', 'width': 10, 'height': 10}],
                    reply_to_message={'message_id': 50, 'photo': [{'file_id': 'quoted-photo'}], 'caption': '原图说明'})
        with patch('attachments.download', AsyncMock(return_value=photo)) as download:
            await self.bridge.dispatch_one(self.bridge.store.pending())
        self.assertEqual(download.await_count, 1)
        self.assertEqual(download.call_args.args[1]['file_id'], 'new-photo')
        method, params = self.codex.call.call_args.args
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['input'][1], {'type': 'localImage', 'path': str(photo)})
        self.assertIn('原图说明', params['input'][-1]['text'])
        self.assertNotIn('quoted-photo', params['input'][-1]['text'])

    async def test_album_with_same_reply_has_one_context_and_original_caption(self):
        photo = self.path / 'photo.png'
        photo.write_bytes(b'\x89PNG\r\n\x1a\n')
        for i in (1, 2):
            self.accept('', i, caption='比较图片' if i == 1 else '', media_group_id='album',
                        photo=[{'file_id': str(i), 'width': 10, 'height': 10}],
                        reply_to_message={'message_id': 50, 'text': '引用原文'})
        self.bridge.store.db.execute('UPDATE inbox SET received_ms=received_ms-3000')
        self.bridge.store.db.commit()
        with patch('attachments.download', AsyncMock(return_value=photo)):
            await self.bridge.dispatch_one(self.bridge.store.pending())
        values = self.codex.call.call_args.args[1]['input']
        self.assertEqual(len([v for v in values if v.get('text', '').startswith('Telegram 引用上下文')]), 1)
        self.assertEqual(len([v for v in values if v['type'] == 'localImage']), 2)
        self.assertIn('比较图片', values[0]['text'])

    async def test_old_rows_without_reply_column_remain_supported_by_input_builder(self):
        rows = [{'text': '旧队列', 'message_json': None}]
        self.assertEqual(await inputs_for(rows, None, self.path), [{'type': 'text', 'text': '旧队列'}])


if __name__ == '__main__':
    unittest.main()
