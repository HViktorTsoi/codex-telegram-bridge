"""Transient Telegram startup failures must not kill the sole receiver."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge import TelegramError
from topics_bridge import TopicRouter


class StartupRetryTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, responses):
        call = AsyncMock(side_effect=responses)
        router = SimpleNamespace(telegram=SimpleNamespace(call=call), last_error=None)
        with patch('topics_bridge.refresh_health_snapshot') as health, \
                patch('topics_bridge.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await TopicRouter.startup_call(router, 'getMe')
        return result, router, call, health, sleep

    async def test_gateway_and_transport_failure_retry_then_clear_startup_error(self):
        for error in (TelegramError('getMe', 502, 'Bad Gateway'), ConnectionError('offline')):
            with self.subTest(error=type(error).__name__):
                result, router, call, health, sleep = await self.invoke([error, {'id': 123}])
                self.assertEqual(result, {'id': 123})
                self.assertIsNone(router.last_error)
                self.assertEqual(call.await_count, 2)
                sleep.assert_awaited_once_with(3)
                health.assert_called_once_with(router)

    async def test_rate_limit_preserves_server_retry_delay(self):
        _, _, _, _, sleep = await self.invoke([TelegramError('getMe', 429, 'limited', retry_after=61), {'id': 123}])
        sleep.assert_awaited_once_with(61)

    async def test_invalid_credentials_and_poller_conflict_are_not_retried(self):
        for code in (401, 409):
            call = AsyncMock(side_effect=TelegramError('getMe', code, 'rejected'))
            router = SimpleNamespace(telegram=SimpleNamespace(call=call), last_error=None)
            with patch('topics_bridge.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(TelegramError):
                    await TopicRouter.startup_call(router, 'getMe')
            call.assert_awaited_once()
            sleep.assert_not_awaited()

    async def test_shutdown_cancellation_is_not_retried(self):
        router = SimpleNamespace(telegram=SimpleNamespace(call=AsyncMock(side_effect=asyncio.CancelledError)), last_error=None)
        with patch('topics_bridge.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await TopicRouter.startup_call(router, 'getMe')
        sleep.assert_not_awaited()
