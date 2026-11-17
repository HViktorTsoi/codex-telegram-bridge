"""Read-only, scope-checked views of native Codex agent trees.

Children inherit access from a freshly verified interactive root, rather than
from the registry's default allocation for unassigned thread IDs. This module
never registers, binds, loads, resumes, or otherwise controls a thread.
"""
from __future__ import annotations

import asyncio
import copy
import re
import time

from bridge import RpcError

class AgentCatalogError(ValueError):
    """An intentionally public error without RPC payloads or private metadata."""


_INTERACTIVE = frozenset(('cli', 'vscode', 'exec', 'appServer', 'unknown'))
_STATUSES = frozenset(('active', 'idle', 'notLoaded', 'systemError'))
_TURN_STATUSES = frozenset(('completed', 'interrupted', 'failed', 'inProgress'))
_READ_METHODS = frozenset(('thread/read', 'thread/list', 'thread/turns/list'))
_LINK = re.compile(r'(?:file://|wss?://|(?:mailto|data):)[^\s<>\]）)]+', re.I)
_SECRET = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|(?:access_token|refresh_token|api_key|token)\s*[=:]\s*[^\s,;]+)', re.I)


def _id(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(ord(character) < 32 for character in value))


def _text(value, limit=1200):
    if not isinstance(value, str):
        return ''
    value = ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)
    value = _SECRET.sub('[已隐藏]', _LINK.sub('[链接]', value)).strip()
    return value if len(value) <= limit else value[:limit - 1] + '…'


def _status(thread):
    status = thread.get('status')
    status = status.get('type') if isinstance(status, dict) else status
    return status if isinstance(status, str) and status in _STATUSES else 'unknown'


def _spawn(thread):
    source = thread.get('source')
    subagent = source.get('subAgent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    if not isinstance(spawn, dict) or not _id(spawn.get('parent_thread_id')):
        raise AgentCatalogError('子代理来源或父子关系无法核对，请刷新后重试。')
    parent = spawn['parent_thread_id']
    if thread.get('parentThreadId') is not None and thread['parentThreadId'] != parent:
        raise AgentCatalogError('子代理父子关系不一致，请刷新后重试。')
    if thread.get('id') == parent:
        raise AgentCatalogError('子代理父子关系存在循环，请刷新后重试。')
    return spawn


class AgentCatalog:
    TTL = 5.0
    MIN_REFRESH = 1.0
    TIMEOUT = 8.0

    def __init__(self, router):
        self.router = router
        self._agents_cache = {}
        self._agents_locks = {}

    def _require(self, root_id=None):
        scope = self.router.session_scope
        scope.require(self.router.root_target['thread_id'])
        if root_id is not None:
            if not _id(root_id):
                raise AgentCatalogError('会话标识无效，请重新打开代理列表。')
            scope.require(root_id)

    @staticmethod
    def _cached(cache, refresh):
        if cache is None:
            return False
        age = time.monotonic() - cache[0]
        return age < (AgentCatalog.MIN_REFRESH if refresh else AgentCatalog.TTL)

    async def _catalog_call(self, method, params, root_id=None):
        if method != 'thread/read':
            raise AgentCatalogError('代理目录只允许读取资料。')
        self._require(root_id)
        try:
            result = await asyncio.wait_for(self.router.catalog_call(method, params), self.TIMEOUT)
        except (RpcError, ConnectionError, OSError, asyncio.TimeoutError):
            raise AgentCatalogError('原生代理服务暂时不可用，请稍后刷新。') from None
        self._require(root_id)
        return result

    async def _raw_call(self, root_id, method, params):
        # The caller has already proved the selected interactive root. Recheck
        # ownership on both sides of every await, including metadata-only reads.
        if method not in _READ_METHODS:
            raise AgentCatalogError('代理目录只允许读取资料。')
        self._require(root_id)
        bridge = self.router.bridges[self.router.root_target['key']]
        if not bridge.ready.is_set():
            raise AgentCatalogError('原生代理服务正在连接，请稍后刷新。')
        try:
            result = await asyncio.wait_for(bridge.codex.call(method, params), self.TIMEOUT)
        except (RpcError, ConnectionError, OSError, asyncio.TimeoutError):
            raise AgentCatalogError('原生代理服务暂时不可用，请稍后刷新。') from None
        self._require(root_id)
        return result

    @staticmethod
    def _metadata(result, thread_id):
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != thread_id:
            raise AgentCatalogError('原生代理资料无效，请刷新后重试。')
        return thread

    @staticmethod
    def _is_root(thread):
        return (isinstance(thread.get('source'), str) and thread['source'] in _INTERACTIVE
                and not thread.get('parentThreadId') and not thread.get('ephemeral')
                and thread.get('canAcceptDirectInput') is not False)

    async def _root(self, root_id):
        result = await self._catalog_call('thread/read', {'threadId': root_id, 'includeTurns': False}, root_id)
        thread = self._metadata(result, root_id)
        if not self._is_root(thread):
            raise AgentCatalogError('所选会话不是可查看的主代理，请重新选择。')
        return thread

    async def _pages(self, call, base):
        rows, seen_cursors, cursor = [], set(), None
        while True:
            params = {**base, **({'cursor': cursor} if cursor is not None else {})}
            result = await call('thread/list', params)
            if (not isinstance(result, dict) or not isinstance(result.get('data'), list)
                    or any(not isinstance(row, dict) or not _id(row.get('id')) for row in result['data'])):
                raise AgentCatalogError('原生代理列表格式无效，请刷新后重试。')
            rows.extend(result['data'])
            cursor = result.get('nextCursor')
            if cursor is None or cursor == '':
                return rows
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise AgentCatalogError('原生代理列表分页异常，无法确认完整结果，请刷新后重试。')
            seen_cursors.add(cursor)

    @staticmethod
    def _updated(thread):
        updated = thread.get('updatedAt')
        return updated if type(updated) in (int, float) else None

    @staticmethod
    def _agent_row(thread, archived, depth):
        spawn = _spawn(thread)
        path = _text(spawn.get('agent_path'), 300) or None
        nickname = _text(thread.get('agentNickname') or spawn.get('agent_nickname'), 100) or None
        name = thread.get('name') or (path.rsplit('/', 1)[-1] if path else None) or nickname
        return {'id': thread['id'], 'name': _text(name, 100) or '未命名代理',
                'parent_id': spawn['parent_thread_id'],
                'role': _text(thread.get('agentRole') or spawn.get('agent_role'), 100) or None,
                'status': _status(thread), 'archived': bool(archived),
                'cwd': _text(thread.get('cwd'), 600) or None,
                'updated_at': AgentCatalog._updated(thread), 'preview': _text(thread.get('preview'), 600),
                'depth': depth, 'nickname': nickname, 'agent_path': path}

    @staticmethod
    def _depths(root_id, rows):
        parents = {ident: _spawn(row[0])['parent_thread_id'] for ident, row in rows.items()}
        depths = {root_id: 0}
        for ident in rows:
            path, visited, current = [], set(), ident
            while current not in depths:
                if current in visited or current not in parents:
                    raise AgentCatalogError('子代理归属链不完整或存在循环，无法确认完整结果，请刷新后重试。')
                visited.add(current)
                path.append(current)
                current = parents[current]
            depth = depths[current]
            for current in reversed(path):
                depth += 1
                depths[current] = depth
        return depths

    async def agents(self, root_id, *, refresh=False):
        self._require(root_id)
        lock = self._agents_locks.setdefault(root_id, asyncio.Lock())
        async with lock:
            await self._root(root_id)
            cache = self._agents_cache.get(root_id)
            if not self._cached(cache, refresh):
                rows = {}
                for archived in (False, True):
                    base = {'ancestorThreadId': root_id, 'sourceKinds': ['subAgentThreadSpawn'],
                            'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc',
                            'archived': archived, 'useStateDbOnly': True}
                    call = lambda method, params: self._raw_call(root_id, method, params)
                    for thread in await self._pages(call, base):
                        if thread['id'] == root_id:
                            raise AgentCatalogError('原生代理列表包含异常归属，请刷新后重试。')
                        _spawn(thread)
                        previous = rows.get(thread['id'])
                        if previous and _spawn(previous[0])['parent_thread_id'] != _spawn(thread)['parent_thread_id']:
                            raise AgentCatalogError('子代理父子关系正在变化，请刷新后重试。')
                        rows[thread['id']] = (thread, archived)
                depths = self._depths(root_id, rows)
                data = [self._agent_row(thread, archived, depths[ident])
                        for ident, (thread, archived) in rows.items()]
                data.sort(key=lambda row: (row['depth'], -(row['updated_at'] or 0), row['id']))
                self._require(root_id)
                self._agents_cache[root_id] = (time.monotonic(), data)
            self._require(root_id)
            return copy.deepcopy(self._agents_cache[root_id][1])

    async def _lineage(self, root_id, agent_id):
        await self._root(root_id)
        if not _id(agent_id) or agent_id == root_id:
            raise AgentCatalogError('代理标识无效，请重新打开代理列表。')
        seen, chain, current = {root_id}, [], agent_id
        while current != root_id:
            if current in seen:
                raise AgentCatalogError('子代理归属链存在循环，请刷新后重试。')
            seen.add(current)
            result = await self._raw_call(root_id, 'thread/read', {'threadId': current, 'includeTurns': False})
            thread = self._metadata(result, current)
            chain.append(thread)
            current = _spawn(thread)['parent_thread_id']
        self._require(root_id)
        return chain[0], len(chain)

    async def detail(self, root_id, agent_id):
        thread, depth = await self._lineage(root_id, agent_id)
        # History is fetched only after metadata proves this exact ID belongs to
        # the selected root. Metadata is checked again after the history await.
        history = await self._raw_call(root_id, 'thread/turns/list',
                                       {'threadId': agent_id, 'limit': 2, 'sortDirection': 'desc', 'itemsView': 'full'})
        current, new_depth = await self._lineage(root_id, agent_id)
        if (depth != new_depth or (depth and _spawn(thread)['parent_thread_id'] != _spawn(current)['parent_thread_id'])):
            raise AgentCatalogError('子代理归属链正在变化，请刷新后重试。')
        if not isinstance(history, dict) or not isinstance(history.get('data'), list):
            raise AgentCatalogError('代理公开消息暂时无法读取，请稍后刷新。')
        turns = history['data'][:2]
        if any(not isinstance(turn, dict) for turn in turns):
            raise AgentCatalogError('代理公开消息格式无效，请稍后刷新。')
        latest_reply, task = '', ''
        for turn in turns:
            items = turn.get('items', [])
            if not isinstance(items, list):
                raise AgentCatalogError('代理公开消息格式无效，请稍后刷新。')
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                if (not latest_reply and item.get('type') == 'agentMessage'
                        and item.get('phase') in (None, 'commentary', 'partial_answer', 'final_answer')
                        and isinstance(item.get('text'), str) and '[external_agent_tool' not in item['text']):
                    latest_reply = _text(item['text'], 1800)
                if not task and item.get('type') == 'userMessage':
                    content = item.get('content', [])
                    if isinstance(content, list):
                        task = _text('\n'.join(part['text'] for part in content if isinstance(part, dict)
                                             and part.get('type') == 'text' and isinstance(part.get('text'), str)), 1200)
        task = task or _text(current.get('preview'), 1200)
        # Native Thread metadata has no archived field. Keep a current tree
        # snapshot's archived marker when known; never invent it from idle.
        cached = self._agents_cache.get(root_id)
        known = next((row for row in cached[1] if row['id'] == agent_id), {}) if self._cached(cached, False) else {}
        result = self._agent_row(current, known.get('archived', False), depth)
        if not known:
            result['archived'] = None
        last_status = turns[0].get('status') if turns else None
        result.update(root_id=root_id, task=task, latest_reply=latest_reply,
                      last_turn_status=last_status if isinstance(last_status, str) and last_status in _TURN_STATUSES else None)
        self._require(root_id)
        return result
