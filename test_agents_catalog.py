import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents_catalog import AgentCatalog, AgentCatalogError
from session_scope import SessionScope, SessionScopeError


def root(ident='root', **extra):
    return {'id': ident, 'name': '主代理', 'source': 'cli', 'cwd': '/work',
            'parentThreadId': None, 'canAcceptDirectInput': True, 'ephemeral': False,
            'status': {'type': 'idle'}, 'updatedAt': 1, **extra}


def child(ident='child', parent='root', **extra):
    return {'id': ident, 'name': None, 'source': {'subAgent': {'thread_spawn': {
                'parent_thread_id': parent, 'depth': 1, 'agent_path': '/root/' + ident,
                'agent_nickname': 'RandomNickname', 'agent_role': 'worker'}}},
            'parentThreadId': parent, 'agentNickname': 'RandomNickname',
            'canAcceptDirectInput': False, 'ephemeral': False, 'cwd': '/work',
            'status': {'type': 'active'}, 'updatedAt': 2, 'preview': '最初任务', **extra}


class AgentCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.scope_file = Path(self.temp.name) / 'scope.json'
        self.registry = {'version': 1, 'scopes': ['general', 'research', 'development'],
                         'default_scope': 'general', 'assignments': {'root': 'research'}}
        self.write_scope()
        self.scope = SessionScope({'session_scope_file': str(self.scope_file), 'session_scope': 'research'})
        self.records = {'root': root(), 'child': child()}
        self.lists = {('roots', False): [self.records['root']], ('roots', True): [],
                      ('root', False): [self.records['child']], ('root', True): []}
        self.turns = []
        self.calls = []
        self.native = AsyncMock(side_effect=self.rpc)
        ready = asyncio.Event()
        ready.set()
        self.router = SimpleNamespace(session_scope=self.scope,
            root_target={'thread_id': 'root', 'key': 'main'},
            targets={'main': {'thread_id': 'root', 'key': 'main', 'name': '绑定名', 'message_thread_id': 11}},
            bridges={'main': SimpleNamespace(ready=ready, codex=SimpleNamespace(call=self.native))})

        async def catalog_call(method, params):
            self.scope.require('root')
            if params.get('threadId'):
                self.scope.require(params['threadId'])
            value = await self.native(method, params)
            if params.get('threadId'):
                self.scope.require(params['threadId'])
            if method == 'thread/list':
                value = {**value, 'data': self.scope.filter_threads(value['data'])}
            return value

        self.router.catalog_call = AsyncMock(side_effect=catalog_call)
        self.catalog = AgentCatalog(self.router)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def write_scope(self):
        temporary = self.scope_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.registry))
        temporary.replace(self.scope_file)

    async def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/read':
            return {'thread': copy.deepcopy(self.records[params['threadId']])}
        if method == 'thread/list':
            group = params.get('ancestorThreadId', 'roots')
            return {'data': copy.deepcopy(self.lists.get((group, params['archived']), []))}
        if method == 'thread/turns/list':
            return {'data': copy.deepcopy(self.turns)}
        raise AssertionError('Read-only catalog invoked ' + method)

    async def test_full_child_pagination_has_no_100_page_cutoff(self):
        async def pages(method, params):
            if method != 'thread/list' or params['archived']:
                return await self.rpc(method, params)
            page = int(params.get('cursor', 0))
            return {'data': [child()], 'nextCursor': str(page + 1) if page < 101 else None}
        self.native.side_effect = pages
        rows = await self.catalog.agents('root')
        self.assertEqual([row['id'] for row in rows], ['child'])
        self.assertEqual(self.native.await_count, 104)

    async def test_duplicate_and_malformed_child_cursor_fail_explicitly(self):
        for cursor in ('repeated', 12):
            with self.subTest(cursor=cursor):
                async def malformed(method, params):
                    if method == 'thread/read':
                        return await self.rpc(method, params)
                    return {'data': [child()], 'nextCursor': cursor}
                self.native.side_effect = malformed
                with self.assertRaisesRegex(AgentCatalogError, '分页异常'):
                    await self.catalog.agents('root')

    async def test_nested_archived_children_inherit_root_access_without_registry_writes(self):
        nested = child('nested', 'child', status={'type': 'notLoaded'})
        self.records['nested'] = nested
        self.lists[('root', True)] = [nested]
        before = self.scope_file.read_bytes()
        rows = await self.catalog.agents('root')
        self.assertEqual([row['id'] for row in rows], ['child', 'nested'])
        self.assertEqual([row['depth'] for row in rows], [1, 2])
        self.assertFalse(rows[0]['archived'])
        self.assertTrue(rows[1]['archived'])
        self.assertEqual(rows[0]['name'], 'child')
        self.assertEqual(rows[0]['role'], 'worker')
        self.assertFalse(self.scope.allows('child'))
        self.assertEqual(self.scope_file.read_bytes(), before)
        self.assertEqual(sum(m == 'thread/list' for m, _ in self.calls), 2)
        for method, params in self.calls:
            if method == 'thread/list':
                self.assertEqual(params['ancestorThreadId'], 'root')
                self.assertEqual(params['sourceKinds'], ['subAgentThreadSpawn'])
                self.assertTrue(params['useStateDbOnly'])
        with self.assertRaises(SessionScopeError):
            await self.router.catalog_call('thread/read', {'threadId': 'child'})

    async def test_tree_paginates_both_archived_states_and_deduplicates(self):
        async def pages(method, params):
            if method != 'thread/list':
                return await self.rpc(method, params)
            if params['archived']:
                return {'data': [child('old', 'child')], 'nextCursor': None}
            if not params.get('cursor'):
                return {'data': [child()], 'nextCursor': 'page-two'}
            return {'data': [child(), child('nested', 'child')], 'nextCursor': None}
        self.native.side_effect = pages
        rows = await self.catalog.agents('root')
        self.assertEqual({row['id'] for row in rows}, {'child', 'nested', 'old'})
        self.assertEqual(next(row for row in rows if row['id'] == 'old')['depth'], 2)

    async def test_invalid_roots_rejected_before_raw_child_reads(self):
        for changed in (child('root', 'foreign'), root(canAcceptDirectInput=False), root(ephemeral=True), root(source=None)):
            with self.subTest(changed=changed):
                self.records['root'] = changed
                self.calls.clear()
                with self.assertRaises(AgentCatalogError):
                    await self.catalog.agents('root')
                self.assertEqual([method for method, _ in self.calls], ['thread/read'])

    async def test_missing_cycle_foreign_and_conflicting_parent_fail_without_leak(self):
        cases = [
            [child('child', 'missing')],
            [child('child', 'other'), child('other', 'child')],
            [child('child', 'foreign')],
            [child(parentThreadId='different')],
            [root('child')],
        ]
        for records in cases:
            with self.subTest(records=records):
                self.catalog._agents_cache.clear()
                self.lists[('root', False)] = records
                with self.assertRaises(AgentCatalogError):
                    await self.catalog.agents('root')
                self.assertEqual(self.catalog._agents_cache, {})

    async def test_scope_revoked_during_root_metadata_await_is_rechecked(self):
        async def revoked(method, params):
            result = await self.rpc(method, params)
            self.registry['assignments']['root'] = 'development'
            self.write_scope()
            return result
        self.native.side_effect = revoked
        with self.assertRaises(SessionScopeError):
            await self.catalog.agents('root')
        self.assertEqual(self.catalog._agents_cache, {})

    async def test_scope_revoked_during_raw_child_list_await_is_rechecked(self):
        async def revoked(method, params):
            result = await self.rpc(method, params)
            if method == 'thread/list':
                self.registry['assignments']['root'] = 'development'
                self.write_scope()
            return result
        self.native.side_effect = revoked
        with self.assertRaises(SessionScopeError):
            await self.catalog.agents('root')
        self.assertEqual(self.catalog._agents_cache, {})

    async def test_cache_coalesces_lists_but_freshly_checks_root(self):
        await asyncio.gather(self.catalog.agents('root'), self.catalog.agents('root'))
        self.assertEqual(sum(m == 'thread/list' for m, _ in self.calls), 2)
        self.assertEqual(sum(m == 'thread/read' for m, _ in self.calls), 2)
        self.records['root']['canAcceptDirectInput'] = False
        with self.assertRaises(AgentCatalogError):
            await self.catalog.agents('root')

    async def test_detail_only_public_text_and_native_turn_status(self):
        self.records['child']['metadata'] = {'url': 'https://metadata-private.test/', 'tokens': 'metadata-credential'}
        self.turns = [{'status': 'interrupted', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': '最新任务 https://private.test/?token=private'}]},
            {'type': 'reasoning', 'text': 'hidden reasoning'},
            {'type': 'commandExecution', 'text': 'private tool output https://tool-private.test/'},
            {'type': 'agentMessage', 'text': '公开答复 https://results.example/report sk-abcdefghijklmnopqrst file:///private/report wss://internal.test/socket data:private', 'phase': 'commentary'},
            {'type': 'agentMessage', 'text': 'private phase', 'phase': 'analysis'},
        ]}]
        before = self.scope_file.read_bytes()
        detail = await self.catalog.detail('root', 'child')
        self.assertEqual(detail['task'], '最新任务 https://private.test/?[已隐藏]')
        self.assertEqual(detail['latest_reply'], '公开答复 https://results.example/report [已隐藏] [链接] [链接] [链接]')
        self.assertEqual(detail['last_turn_status'], 'interrupted')
        self.assertEqual(detail['status'], 'active')
        self.assertIsNone(detail['archived'])
        self.assertEqual(self.scope_file.read_bytes(), before)
        serialized = json.dumps(detail)
        for hidden in ('hidden reasoning', 'private tool output', 'private phase', 'token=private', 'sk-abcdefghijklmnopqrst',
                       'tokens', 'metadata-private.test', 'metadata-credential', 'tool-private.test', 'file://', 'wss://', 'data:private'):
            self.assertNotIn(hidden, serialized)
        history = next(params for method, params in self.calls if method == 'thread/turns/list')
        self.assertEqual(history, {'threadId': 'child', 'limit': 2, 'sortDirection': 'desc', 'itemsView': 'full'})
        self.assertTrue(all(m in ('thread/read', 'thread/list', 'thread/turns/list') for m, _ in self.calls))

    async def test_detail_nested_lineage_fresh_even_if_cache_exists(self):
        nested = child('nested', 'child')
        self.records['nested'] = nested
        self.lists[('root', True)] = [nested]
        await self.catalog.agents('root')
        self.turns = [{'status': 'completed', 'items': [{'type': 'agentMessage', 'text': '公开最终回复'}]}]
        result = await self.catalog.detail('root', 'nested')
        self.assertEqual((result['depth'], result['archived'], result['last_turn_status']), (2, True, 'completed'))
        self.records['child'] = child('child', 'foreign')
        self.records['foreign'] = root('foreign')
        calls_before = sum(m == 'thread/turns/list' for m, _ in self.calls)
        with self.assertRaises(AgentCatalogError):
            await self.catalog.detail('root', 'nested')
        self.assertEqual(sum(m == 'thread/turns/list' for m, _ in self.calls), calls_before)

    async def test_detail_scope_revoked_during_history_read(self):
        async def revoked(method, params):
            result = await self.rpc(method, params)
            if method == 'thread/turns/list':
                self.registry['assignments']['root'] = 'development'
                self.write_scope()
            return result
        self.native.side_effect = revoked
        with self.assertRaises(SessionScopeError):
            await self.catalog.detail('root', 'child')

    async def test_detail_rechecks_chain_after_history_await(self):
        self.records['foreign'] = root('foreign')
        async def moved(method, params):
            result = await self.rpc(method, params)
            if method == 'thread/turns/list':
                self.records['child'] = child('child', 'foreign')
            return result
        self.native.side_effect = moved
        with self.assertRaises(AgentCatalogError):
            await self.catalog.detail('root', 'child')

    async def test_idle_is_not_invented_as_completed(self):
        self.records['child']['status'] = {'type': 'idle'}
        result = await self.catalog.detail('root', 'child')
        self.assertEqual(result['status'], 'idle')
        self.assertIsNone(result['last_turn_status'])
        self.assertEqual(result['task'], '最初任务')

    async def test_previous_turn_reply_when_latest_turn_has_no_public_reply(self):
        self.turns = [{'status': 'inProgress', 'items': [{'type': 'reasoning', 'text': 'hidden'}]},
                      {'status': 'completed', 'items': [{'type': 'agentMessage', 'text': '上次公开回复'},
                       {'type': 'agentMessage', 'text': '[external_agent_tool private]'}]}]
        result = await self.catalog.detail('root', 'child')
        self.assertEqual(result['latest_reply'], '上次公开回复')
        self.assertEqual(result['last_turn_status'], 'inProgress')

    async def test_connection_and_malformed_responses_are_safe_errors(self):
        self.native.side_effect = ConnectionError('https://private.test token=secret')
        with self.assertRaises(AgentCatalogError) as error:
            await self.catalog.agents('root')
        self.assertNotIn('private', str(error.exception))
        async def malformed(method, params):
            return await self.rpc(method, params) if method == 'thread/read' else {'data': None}
        self.native.side_effect = malformed
        with self.assertRaises(AgentCatalogError):
            await self.catalog.agents('root')


if __name__ == '__main__':
    unittest.main()
