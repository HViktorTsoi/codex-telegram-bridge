from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from bus_reminder import HK, Monitor, choose_last_bus, due, in_window, operating_day


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


if __name__ == "__main__":
    unittest.main()
