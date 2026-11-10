import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bus_reminder import ALERT_VOICE, LOCAL_ALERT_VOICE, HK, Monitor, choose_last_bus, due, in_window, operating_day


def data(now, rows):
    return {"generated_timestamp": now.isoformat(), "data": rows}


def row(now, seq=29, minutes=12, note="", direction="I"):
    return {"route": "72", "dir": direction, "service_type": 1, "seq": seq,
            "eta": (now + timedelta(minutes=minutes)).isoformat() if minutes is not None else None,
            "data_timestamp": now.isoformat(), "rmk_tc": note}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 31, 0, 30, tzinfo=HK)
        self.origin = data(self.now, [row(self.now, seq=1, minutes=None, note="最後班次已過")])

    def test_midnight_window_and_operating_day(self):
        config = {"start_minute": 1425, "end_minute": 120}
        for hour, minute, expected in [(23, 44, False), (23, 45, True), (0, 30, True), (1, 59, True), (2, 0, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(in_window(self.now.replace(hour=hour, minute=minute), config), expected)
        self.assertEqual(operating_day(self.now), "2026-10-30")
        self.assertTrue(in_window(self.now.astimezone(timezone.utc), config))

    def test_threshold_crossing_and_passed_bus(self):
        for minutes, expected in [(13, False), (12, True), (11, True), (0, False), (-1, False)]:
            self.assertEqual(due({"eta": self.now + timedelta(minutes=minutes)}, self.now), expected)

    def test_one_eta_alone_does_not_establish_a_last_bus(self):
        empty_origin = data(self.now, [row(self.now, seq=1, minutes=10)])
        result, _, _ = choose_last_bus(empty_origin, data(self.now, [row(self.now)]), 29, self.now, {})
        self.assertIsNone(result)

    def test_cross_check_requires_two_distinct_fresh_snapshots(self):
        stop = data(self.now, [row(self.now)])
        estimate, previous, reason = choose_last_bus(self.origin, stop, 29, self.now, {})
        self.assertIsNone(estimate)
        self.assertEqual(reason, "awaiting_second_snapshot")
        estimate, repeated, _ = choose_last_bus(self.origin, stop, 29, self.now + timedelta(seconds=30), previous)
        self.assertIsNone(estimate)
        self.assertEqual(repeated["count"], 1)
        later = self.now + timedelta(minutes=1)
        stop2 = data(later, [row(later, minutes=11)])
        estimate, _, _ = choose_last_bus(self.origin, stop2, 29, later, repeated)
        self.assertEqual(estimate["eta"], self.now + timedelta(minutes=12))

    def test_stale_data_wrong_direction_and_wrong_stop_do_not_trigger(self):
        for r in [row(self.now, direction="O", note="尾班車"), row(self.now, seq=28, note="尾班車"),
                  row(self.now - timedelta(minutes=5), minutes=17, note="尾班車")]:
            estimate, _, _ = choose_last_bus(self.origin, data(self.now, [r]), 29, self.now, {})
            self.assertIsNone(estimate)

    def test_multiple_remaining_buses_are_not_mistaken_for_the_last(self):
        estimate, _, _ = choose_last_bus(self.origin, data(self.now, [row(self.now), row(self.now, minutes=30)]), 29, self.now, {})
        self.assertIsNone(estimate)

    def test_explicit_final_marker_selects_its_own_eta(self):
        stop = data(self.now, [row(self.now, minutes=3), row(self.now, minutes=12, note="尾班車")])
        estimate, _, _ = choose_last_bus({}, stop, 29, self.now, {})
        self.assertEqual(estimate["eta"], self.now + timedelta(minutes=12))
        self.assertEqual(estimate["evidence"], "explicit_final_bus_remark")

    def test_final_bus_already_departed_is_not_an_alert(self):
        stop = data(self.now, [row(self.now, minutes=None, note="最後班次已過")])
        estimate, _, reason = choose_last_bus(self.origin, stop, 29, self.now, {})
        self.assertIsNone(estimate)
        self.assertEqual(reason, "final_bus_already_departed")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        bridge = self.path / "bridge.json"
        bridge.write_text(json.dumps({"owner_id": "123", "telegram_state_dir": str(self.path)}))
        (self.path / "config.json").write_text(json.dumps({"bridge_config": str(bridge), "lead_minutes": 12,
            "origin_stop": "origin", "boarding_stop": "boarding"}))
        self.now = datetime(2026, 10, 31, 0, 30, tzinfo=HK)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def monitor(self):
        monitor = Monitor(self.path)

        async def fetch(path):
            return data(self.now, [row(self.now, note="尾班車")]) if "boarding" in path else data(self.now, [])

        async def deliver(estimate, now, day):
            monitor.progress["alerted_day"] = day
            monitor.progress["message_id"] = monitor.progress.get("message_id", 0) + 1
            monitor.save()

        monitor.fetch, monitor.deliver = fetch, deliver
        return monitor

    async def test_reminder_is_sent_once_per_night_across_restarts(self):
        monitor = self.monitor()
        await monitor.check(self.now)
        self.assertEqual(monitor.progress["message_id"], 1)
        restarted = self.monitor()
        await restarted.check(self.now)
        self.assertEqual(restarted.progress["message_id"], 1)
        self.now += timedelta(days=1)
        await restarted.check(self.now)
        self.assertEqual(restarted.progress["message_id"], 2)

    async def test_dry_run_never_sends_or_suppresses_the_real_nightly_alert(self):
        monitor = self.monitor()
        await monitor.check(self.now, send=False)
        self.assertNotIn("alerted_day", monitor.progress)
        restarted = self.monitor()
        await restarted.check(self.now)
        self.assertEqual(restarted.progress["message_id"], 1)


class LocalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {"delivery_mode": "local_audio", "audio_device": "default:CARD=Generic",
                       "lead_minutes": 12, "start_minute": 20, "end_minute": 60,
                       "interval_seconds": 60, "origin_stop": "origin", "boarding_stop": "boarding"}
        (self.path / "config.json").write_text(json.dumps(self.config))
        self.now = datetime(2026, 10, 31, 0, 30, tzinfo=HK)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def monitor(self):
        monitor = Monitor(self.path)
        monitor.prepare_voice = AsyncMock()

        async def fetch(path):
            return data(self.now, [row(self.now, note="尾班車")]) if "boarding" in path else data(self.now, [])

        monitor.fetch = fetch
        return monitor

    def estimate(self):
        return {"eta": self.now + timedelta(minutes=12)}

    async def test_local_mode_has_no_bridge_token_or_allowlist_dependency(self):
        monitor = self.monitor()
        self.assertIsNone(monitor.bridge_config)
        self.assertIsNone(monitor.owner)
        self.assertIsNone(monitor.telegram_dir)
        with patch.object(monitor, "token", side_effect=AssertionError("Must not read a token")), \
                patch("bus_reminder.Telegram", side_effect=AssertionError("Must not create Telegram")):
            monitor.configure_delivery()
            self.assertTrue(monitor.allowed())
            self.assertIsNone(monitor.telegram)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await monitor.send_voice(self.path / "missing.mp3", "must never upload")

    async def test_successful_audio_records_a_local_delivery_only_after_playback(self):
        monitor = self.monitor()
        monitor.allowed = Mock(side_effect=AssertionError("Local audio must not use Telegram policy"))
        monitor.send_voice = AsyncMock(side_effect=AssertionError("Must not send Telegram voice"))
        monitor.telegram = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("Must not notify Telegram")))
        day = operating_day(self.now)

        async def play(path, device):
            self.assertNotIn("alerted_day", monitor.progress)
            self.assertEqual(path, self.path / "alert-local.mp3")
            self.assertEqual(device, "default:CARD=Generic")

        with patch("bus_reminder.play_audio", new=AsyncMock(side_effect=play)) as player:
            await monitor.deliver(self.estimate(), self.now, day)
        player.assert_awaited_once()
        monitor.send_voice.assert_not_awaited()
        monitor.telegram.call.assert_not_awaited()
        monitor.prepare_voice.assert_awaited_once_with(self.path / "alert-local.mp3", LOCAL_ALERT_VOICE)
        self.assertNotIn("下面的文字", monitor.prepare_voice.await_args.args[1])
        self.assertEqual(monitor.progress["alerted_day"], day)
        self.assertEqual(monitor.progress["mode"], "local_audio")
        self.assertIsNone(monitor.progress["message_id"])
        self.assertEqual(json.loads((self.path / "progress.json").read_text())["mode"], "local_audio")

    async def test_audio_or_synthesis_failure_never_marks_success_or_falls_back_to_telegram(self):
        for stage in ("synthesis", "playback"):
            with self.subTest(stage=stage):
                monitor = self.monitor()
                monitor.send_voice = AsyncMock(side_effect=AssertionError("Must not send Telegram voice"))
                monitor.telegram = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("Must not notify Telegram")))
                if stage == "synthesis":
                    monitor.prepare_voice.side_effect = RuntimeError("audio unavailable")
                with patch("bus_reminder.play_audio", new=AsyncMock(
                        side_effect=RuntimeError("audio unavailable") if stage == "playback" else None)) as player:
                    with self.assertRaisesRegex(RuntimeError, "audio unavailable"):
                        await monitor.deliver(self.estimate(), self.now, operating_day(self.now))
                self.assertNotIn("alerted_day", monitor.progress)
                self.assertNotIn("mode", monitor.progress)
                monitor.send_voice.assert_not_awaited()
                monitor.telegram.call.assert_not_awaited()
                if stage == "synthesis":
                    player.assert_not_awaited()

    async def test_actual_local_delivery_is_deduplicated_across_restarts(self):
        with patch("bus_reminder.play_audio", new=AsyncMock()) as player:
            first = self.monitor()
            await first.check(self.now)
            restarted = self.monitor()
            await restarted.check(self.now)
            player.assert_awaited_once()
            self.now += timedelta(days=1)
            await restarted.check(self.now)
            self.assertEqual(player.await_count, 2)
        self.assertEqual(restarted.progress["mode"], "local_audio")

    async def test_local_dry_run_never_plays_or_consumes_the_nightly_alert(self):
        monitor = self.monitor()
        with patch("bus_reminder.play_audio", new=AsyncMock()) as player:
            await monitor.check(self.now, send=False)
        player.assert_not_awaited()
        monitor.prepare_voice.assert_not_awaited()
        self.assertNotIn("alerted_day", monitor.progress)

    async def test_local_alert_uses_a_separate_cache_and_never_reuses_telegram_instructions(self):
        old = self.path / "alert.mp3"
        old_bytes = b"existing Telegram clip" * 100
        old.write_bytes(old_bytes)
        monitor = Monitor(self.path)
        calls = []
        local_bytes = b"local speech audio" * 100

        class Response:
            headers = {"Content-Type": "audio/mpeg"}

            def raise_for_status(self):
                pass

            async def read(self):
                return local_bytes

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()

        monitor.session = Session()
        path = await monitor.prepare_alert()
        self.assertEqual(path, self.path / "alert-local.mp3")
        self.assertEqual(path.read_bytes(), local_bytes)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["params"]["q"], LOCAL_ALERT_VOICE)
        self.assertNotIn("下面的文字", calls[0][1]["params"]["q"])
        self.assertEqual(await monitor.prepare_alert(), path)
        self.assertEqual(len(calls), 1, "the local cache should also be reused on later starts")

    async def test_test_voice_plays_the_test_clip_without_changing_existing_daily_state(self):
        progress = {"alerted_day": operating_day(self.now), "mode": "voice", "message_id": 1234}
        path = self.path / "progress.json"
        path.write_text(json.dumps(progress))
        before = path.read_bytes()
        monitor = self.monitor()
        monitor.save = Mock(side_effect=AssertionError("Tests must not consume the real alert"))
        with patch("bus_reminder.play_audio", new=AsyncMock()) as player, \
                patch("bus_reminder.Telegram", side_effect=AssertionError("Must not create Telegram")):
            result = await monitor.test_voice()
        player.assert_awaited_once_with(self.path / "test.mp3", "default:CARD=Generic")
        self.assertEqual(result, {"delivery_mode": "local_audio", "test_audio_played": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(monitor.progress, progress)
        self.assertIn("这不是正在到站", monitor.prepare_voice.await_args.args[1])

    async def test_local_run_startup_and_repeated_errors_never_use_telegram(self):
        monitor = self.monitor()
        monitor.validate_route = AsyncMock()
        monitor.check = AsyncMock(side_effect=RuntimeError("ETA unavailable"))
        monitor.token = Mock(side_effect=AssertionError("Must not read Telegram credentials"))
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with patch("bus_reminder.in_window", return_value=True), \
                patch("bus_reminder.Telegram", side_effect=AssertionError("Must not create Telegram")), \
                patch("bus_reminder.asyncio.sleep", new=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.run()
        monitor.token.assert_not_called()
        monitor.prepare_voice.assert_awaited_once_with(self.path / "alert-local.mp3", LOCAL_ALERT_VOICE)
        self.assertIsNone(monitor.telegram)
        self.assertEqual(monitor.check.await_count, 3)
        self.assertEqual(monitor.progress["failures"], 3)
        self.assertNotIn("error_notice_day", monitor.progress)
        self.assertNotIn("alerted_day", monitor.progress)
        health = json.loads((self.path / "health.json").read_text())
        self.assertEqual(health["delivery_mode"], "local_audio")
        self.assertEqual(health["audio_device"], "default:CARD=Generic")
        self.assertEqual(health["last_error"], "RuntimeError")

    async def test_switching_from_telegram_to_local_clears_the_client(self):
        monitor = self.monitor()
        monitor.telegram = object()
        with patch.object(monitor, "token", side_effect=AssertionError("Must not read Telegram credentials")):
            monitor.configure_delivery()
        self.assertIsNone(monitor.telegram)

    async def test_health_distinguishes_current_local_mode_from_historical_telegram_delivery(self):
        monitor = self.monitor()
        monitor.progress.update(mode="voice", message_id=2348)
        monitor.health()
        health = json.loads((self.path / "health.json").read_text())
        self.assertEqual(health["delivery_mode"], "local_audio")
        self.assertEqual(health["audio_device"], "default:CARD=Generic")
        self.assertEqual(health["mode"], "voice")
        self.assertEqual(health["last_delivery_mode"], "voice")


class TelegramDeliveryCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = DeliveryTests.asyncSetUp
    asyncTearDown = DeliveryTests.asyncTearDown

    async def test_omitted_or_explicit_telegram_mode_keeps_voice_delivery(self):
        (self.path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["123"]}))
        for mode in (None, "telegram"):
            with self.subTest(mode=mode):
                config = json.loads((self.path / "config.json").read_text())
                if mode:
                    config["delivery_mode"] = mode
                (self.path / "config.json").write_text(json.dumps(config))
                monitor = Monitor(self.path)
                monitor.prepare_voice = AsyncMock()
                monitor.send_voice = AsyncMock(return_value={"message_id": 77})
                with patch("bus_reminder.play_audio", new=AsyncMock()) as player:
                    await monitor.deliver({"eta": self.now + timedelta(minutes=12)}, self.now, operating_day(self.now))
                player.assert_not_awaited()
                monitor.send_voice.assert_awaited_once()
                self.assertEqual(monitor.progress["mode"], "voice")
                self.assertEqual(monitor.progress["message_id"], 77)
                monitor.prepare_voice.assert_awaited_once_with(self.path / "alert.mp3", ALERT_VOICE)

    async def test_telegram_keeps_its_existing_cache_and_original_caption_instructions(self):
        monitor = Monitor(self.path)
        old = self.path / "alert.mp3"
        old_bytes = b"cached Telegram audio" * 100
        old.write_bytes(old_bytes)
        self.assertEqual(monitor.alert_clip(), (old, ALERT_VOICE))
        self.assertIn("下面的文字", ALERT_VOICE)
        with patch.object(monitor, "session", SimpleNamespace(get=Mock(side_effect=AssertionError("Cached clip must be reused")))):
            self.assertEqual(await monitor.prepare_alert(), old)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertFalse((self.path / "alert-local.mp3").exists())

    async def test_telegram_voice_failure_still_falls_back_to_text(self):
        (self.path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["123"]}))
        monitor = Monitor(self.path)
        monitor.prepare_voice = AsyncMock()
        monitor.send_voice = AsyncMock(side_effect=RuntimeError("voice unavailable"))
        monitor.telegram = SimpleNamespace(call=AsyncMock(return_value={"message_id": 88}))
        with patch("bus_reminder.play_audio", new=AsyncMock()) as player:
            await monitor.deliver({"eta": self.now + timedelta(minutes=12)}, self.now, operating_day(self.now))
        player.assert_not_awaited()
        monitor.telegram.call.assert_awaited_once()
        self.assertEqual(monitor.progress["mode"], "text_fallback")
        self.assertEqual(monitor.progress["message_id"], 88)


if __name__ == "__main__":
    unittest.main()
