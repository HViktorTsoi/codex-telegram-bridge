import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from legacy_questions import read_question_source
from topics_bridge import ExactCodex


class LegacyQuestionSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.path = self.sessions / 'rollout.jsonl'
        self.records = [
            {'type': 'session_meta', 'payload': {'id': 'native'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'original'}},
            {'type': 'response_item', 'payload': {
                'type': 'function_call', 'name': 'request_user_input_async', 'call_id': 'call_exact',
                'arguments': json.dumps({'questions': [
                    {'title': 'Pick one', 'options': [{'label': 'A', 'description': 'Private detail'}, 'B']}],
                    'extra': 'Private extra'})}},
            {'type': 'response_item', 'payload': {
                'type': 'function_call_output', 'call_id': 'call_exact', 'output': 'Private output'}},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, records=None, **kwargs):
        self.path.write_text('\n'.join(json.dumps(record) for record in
                                       (self.records if records is None else records)) + '\n')
        return read_question_source(str(self.path), 'native', 'original', 'call_exact',
                                    roots=[self.sessions], **kwargs)

    def test_reads_exact_source_and_normalizes_labels_without_exposing_other_data(self):
        self.assertEqual(self.read(), {'id': 'call_exact', 'type': 'agentMessage', 'delivery': 'async',
                                      'questions': [{'title': 'Pick one', 'options': ['A', 'B']}]})

    def test_session_turn_tool_and_call_id_must_match(self):
        for record_index, key, value in [(0, 'id', 'different'), (1, 'turn_id', 'different'),
                                         (2, 'name', 'other_tool'), (2, 'call_id', 'other_call')]:
            with self.subTest(key=key):
                records = copy.deepcopy(self.records)
                records[record_index]['payload'][key] = value
                self.assertIsNone(self.read(records))

    def test_outputs_and_same_text_under_other_id_are_not_question_sources(self):
        records = copy.deepcopy(self.records)
        records[2]['payload']['call_id'] = 'call_same_text'
        self.assertIsNone(self.read(records))
        self.assertIsNone(self.read(self.records[1:]))

    def test_secret_malformed_or_conflicting_questions_are_rejected(self):
        for arguments in ['not JSON', json.dumps({'questions': [{'title': 'Secret', 'isSecret': True}]}),
                          json.dumps({'questions': []})]:
            with self.subTest(arguments=arguments):
                records = copy.deepcopy(self.records)
                records[2]['payload']['arguments'] = arguments
                self.assertIsNone(self.read(records))
        records = copy.deepcopy(self.records)
        duplicate = copy.deepcopy(records[2])
        duplicate['payload']['arguments'] = json.dumps({'questions': [{'title': 'Changed'}]})
        records.append(duplicate)
        self.assertIsNone(self.read(records))

    def test_absolute_regular_file_inside_allowed_roots_is_required(self):
        self.read()
        self.assertIsNone(read_question_source('rollout.jsonl', 'native', 'original', 'call_exact',
                                              roots=[self.sessions]))
        self.assertIsNone(read_question_source(str(self.path), 'native', 'original', 'call_exact',
                                              roots=[self.root / 'other']))
        outside = self.root / 'outside.jsonl'
        outside.write_text(self.path.read_text())
        link = self.sessions / 'linked.jsonl'
        link.symlink_to(outside)
        self.assertIsNone(read_question_source(str(link), 'native', 'original', 'call_exact',
                                              roots=[self.sessions]))
        self.assertIsNone(read_question_source(str(self.sessions), 'native', 'original', 'call_exact',
                                              roots=[self.sessions]))

    def test_default_roots_allow_local_sessions_and_archived_sessions(self):
        for directory in ('sessions', 'archived_sessions'):
            with self.subTest(directory=directory):
                self.read()
                local = self.root / '.codex' / directory / self.path.name
                local.parent.mkdir(parents=True)
                local.write_text(self.path.read_text())
                with patch('legacy_questions.Path.home', return_value=self.root), patch.dict(
                        'os.environ', {'CODEX_HOME': ''}):
                    source = read_question_source(str(local), 'native', 'original', 'call_exact')
                self.assertIsNotNone(source)


class ExactQuestionSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_checks_thread_id_and_offloads_file_scan(self):
        client = ExactCodex('/unused', 'native')
        client.call = AsyncMock(return_value={'thread': {'id': 'native', 'path': '/home/test/rollout.jsonl'}})
        expected = {'id': 'call_exact', 'questions': []}
        with patch('legacy_questions.read_question_source', return_value=expected) as read:
            self.assertEqual(await client.question_source('turn', 'call_exact'), expected)
        client.call.assert_awaited_once_with('thread/read', {'threadId': 'native', 'includeTurns': False})
        read.assert_called_once_with('/home/test/rollout.jsonl', 'native', 'turn', 'call_exact')

    async def test_other_thread_path_is_never_read(self):
        client = ExactCodex('/unused', 'native')
        client.call = AsyncMock(return_value={'thread': {'id': 'other', 'path': '/home/test/rollout.jsonl'}})
        with patch('legacy_questions.read_question_source') as read:
            self.assertIsNone(await client.question_source('turn', 'call_exact'))
        read.assert_not_called()

    async def test_connection_change_during_read_is_not_accepted(self):
        client = ExactCodex('/unused', 'native')
        client.call = AsyncMock(return_value={'thread': {'id': 'native', 'path': '/home/test/rollout.jsonl'}})

        def read(*args):
            client.turn_state.reset()
            return {'id': 'call_exact'}

        with patch('legacy_questions.read_question_source', side_effect=read):
            with self.assertRaises(ConnectionError):
                await client.question_source('turn', 'call_exact')


if __name__ == '__main__':
    unittest.main()
