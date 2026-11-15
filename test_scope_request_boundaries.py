"""Scope changes between awaited reads/requests must block the next side effect."""
import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge import Bridge, GuardedTelegram, TelegramError, TelegramScopeError
import test_session_scope_integration as fixtures


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')


class ScopeRequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ScopeIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.ScopeIntegrationTests.asyncTearDown
    enable_scope = fixtures.ScopeIntegrationTests.enable_scope
    write_scope = fixtures.ScopeIntegrationTests.write_scope
    moved = fixtures.ScopeIntegrationTests.moved

    def start_with_transport(self, transport):
        bridge = self.router.bridges['instagram']
        self.router.telegram, self.router.username = transport, 'test_bot'
        # Exercise the real startup wiring without launching workers or sockets.
        self.router.spawn = lambda coroutine: coroutine.close()
        self.router.start_target(bridge)
        return bridge

    def accept(self, bridge, update_id, text):
        bridge.store.accept({'update_id': update_id}, {
            'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'message_thread_id': bridge.target['message_thread_id'], 'text': text,
        })
        return bridge.store.pending()

    def assert_denied_unchanged(self, bridge, before):
        after = bridge.store.db.execute('SELECT * FROM inbox WHERE id=?', (before['id'],)).fetchone()
        self.assertEqual(after['state'], 'denied')
        self.assertEqual(after['client_id'], before['client_id'])
        self.assertIsNone(after['turn_id'])
        self.assertEqual(bridge.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    async def test_start_target_guards_only_scoped_bridge_and_forwards_rich_capability(self):
        self.enable_scope()
        transport = SimpleNamespace(call=AsyncMock(), upload=AsyncMock(), rich_supported=None, marker=object())
        bridge = self.start_with_transport(transport)
        self.assertIs(self.router.telegram, transport)
        self.assertIsInstance(bridge.telegram, GuardedTelegram)
        self.assertIs(bridge.telegram.marker, transport.marker)
        bridge.telegram.rich_supported = False
        self.assertIs(transport.rich_supported, False)
        transport.rich_supported = True
        self.assertIs(bridge.telegram.rich_supported, True)
        self.registry.unlink()
        with self.assertRaises(TelegramScopeError) as rejected:
            await bridge.telegram.call('sendMessage', chat_id='123', text='blocked')
        self.assertFalse(rejected.exception.uncertain)
        transport.call.assert_not_awaited()
        # The router's global client remains available for safe control replies.
        await self.router.telegram.call('answerCallbackQuery', callback_query_id='control')
        transport.call.assert_awaited_once()

    async def test_unscoped_target_keeps_original_transport(self):
        transport = SimpleNamespace(call=AsyncMock(), upload=AsyncMock())
        bridge = self.start_with_transport(transport)
        self.assertIs(bridge.telegram, transport)

    async def test_rich_400_after_scope_move_does_not_send_plain_fallback(self):
        self.enable_scope()
        async def reject_and_move(method, **params):
            self.assertEqual(method, 'sendRichMessage')
            self.moved('instagram-native')
            raise TelegramError(method, 400, "can't parse entities")
        transport = SimpleNamespace(call=AsyncMock(side_effect=reject_and_move), upload=AsyncMock())
        bridge = self.start_with_transport(transport)
        bridge.store.enqueue_at('turn:test-rich:reply', '123', '**Original body**', 22)
        row = bridge.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertIsNotNone(row['rich_text'])
        with self.assertRaises(TelegramScopeError):
            await bridge.send_one(row)
        self.assertEqual([call.args[0] for call in transport.call.await_args_list], ['sendRichMessage'])
        transport.upload.assert_not_awaited()
        self.assertIsNone(bridge.store.db.execute('SELECT message_id FROM outbox').fetchone()[0])

    async def test_photo_400_after_scope_move_does_not_upload_document_fallback(self):
        self.enable_scope()
        async def reject_and_move(method, *args, **params):
            self.assertEqual(method, 'sendPhoto')
            self.moved('instagram-native')
            raise TelegramError(method, 400, 'PHOTO_INVALID_DIMENSIONS')
        transport = SimpleNamespace(call=AsyncMock(), upload=AsyncMock(side_effect=reject_and_move))
        bridge = self.start_with_transport(transport)
        image = self.path / 'chart.png'
        image.write_bytes(PNG)
        bridge.store.enqueue_at('turn:test-photo:reply', '123', f'![Chart](<{image}>)', 22)
        row = bridge.store.db.execute('SELECT * FROM outbox WHERE media_json IS NOT NULL').fetchone()
        self.assertIsNotNone(row)
        with self.assertRaises(TelegramScopeError):
            await bridge.send_one(row)
        self.assertEqual([call.args[0] for call in transport.upload.await_args_list], ['sendPhoto'])
        transport.call.assert_not_awaited()

    async def test_single_bridge_run_installs_guard_without_reading_real_credentials(self):
        self.enable_scope()
        directory = self.path / 'single'
        directory.mkdir()
        bridge = Bridge(dict(self.router.config), directory)
        transport = SimpleNamespace(call=AsyncMock(side_effect=ConnectionError('offline startup check')))
        try:
            with patch('bridge.Telegram', return_value=transport), patch.object(bridge, 'token', return_value='unused'):
                with self.assertRaises(ConnectionError):
                    await bridge.run()
            self.assertIsInstance(bridge.telegram, GuardedTelegram)
            transport.call.assert_awaited_once_with('getMe')
        finally:
            bridge.store.db.close()

    async def test_model_and_effort_do_not_mutate_after_scope_moves_during_metadata_read(self):
        self.enable_scope()
        bridge = self.router.bridges['instagram']
        for index, (command, stage) in enumerate([
            ('/model test-model', 'settings'), ('/model test-model', 'models'),
            ('/effort high', 'settings'), ('/effort high', 'models'),
        ]):
            with self.subTest(command=command, stage=stage):
                self.assignments[bridge.thread_id] = 'general'
                self.write_scope()
                async def settings():
                    if stage == 'settings':
                        self.moved(bridge.thread_id)
                    return {'model': 'test-model', 'reasoningEffort': 'low'}
                async def models():
                    if stage == 'models':
                        self.moved(bridge.thread_id)
                    return [{'id': 'test-model', 'defaultReasoningEffort': 'low',
                             'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]}]
                bridge.codex = SimpleNamespace(settings=AsyncMock(side_effect=settings),
                    models=AsyncMock(side_effect=models), call=AsyncMock())
                before = self.accept(bridge, 800 + index, command)
                await bridge.dispatch_one(before)
                bridge.codex.call.assert_not_awaited()
                if stage == 'settings':
                    bridge.codex.models.assert_not_awaited()
                self.assert_denied_unchanged(bridge, before)

    async def test_legacy_stop_and_stop_extra_do_not_interrupt_after_history_read_moves_scope(self):
        self.enable_scope()
        bridge = self.router.bridges['instagram']
        other = self.router.bridges['meeting']
        unrelated = self.accept(other, 900, 'unrelated task')
        other.store.mark(unrelated['id'], 'running', 'other-active-turn')
        for index, text in enumerate(['/stop', '/stop extra']):
            with self.subTest(text=text):
                self.assignments[bridge.thread_id] = 'general'
                self.write_scope()
                async def turns(limit):
                    self.moved(bridge.thread_id)
                    return [{'id': 'original-turn', 'status': 'inProgress'}]
                bridge.codex = SimpleNamespace(turns=AsyncMock(side_effect=turns), call=AsyncMock())
                before = self.accept(bridge, 901 + index, text)
                await bridge.dispatch_one(before)
                bridge.codex.call.assert_not_awaited()
                self.assert_denied_unchanged(bridge, before)
        after = other.store.db.execute('SELECT state,turn_id,client_id FROM inbox WHERE id=900').fetchone()
        self.assertEqual(tuple(after), ('running', 'other-active-turn', unrelated['client_id']))
