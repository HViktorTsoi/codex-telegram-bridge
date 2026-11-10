#!/usr/bin/env python3
"""Daily KMB 72 last-bus monitor with local audio or Telegram delivery."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timedelta
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import time
from zoneinfo import ZoneInfo

import aiohttp

from bridge import Telegram, TelegramError, permitted
from local_audio import play_audio

HK = ZoneInfo("Asia/Hong_Kong")
STATE = Path.home() / ".local/state/kmb72-reminder"
BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
LOG = logging.getLogger("kmb72")
ALERT_VOICE = "九巴七十二路，从赤泥坪往广福道方向的末班车，预计十二分钟内到站，请准备出发。具体到站时间，请查看这条语音下面的文字。"
LOCAL_ALERT_VOICE = "九巴七十二路，从赤泥坪往广福道方向的末班车，预计十二分钟内到站，请准备出发。"
TEST_VOICE = "这是九巴七十二路末班车提醒的语音测试。这不是正在到站的通知。正式提醒会在末班车预计到达赤泥坪前十二分钟左右发送。"


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def operating_day(now):
    return (now.astimezone(HK) - timedelta(hours=5)).date().isoformat()


def in_window(now, config):
    now = now.astimezone(HK)
    minute = now.hour * 60 + now.minute
    start, end = config["start_minute"], config["end_minute"]
    return minute >= start or minute < end if start > end else start <= minute < end


def remark(row):
    return " ".join(str(row.get(key) or "") for key in ["rmk_tc", "rmk_sc", "rmk_en"]).lower()


def has_departed(row):
    text = remark(row)
    return any(key in text for key in ["最後班次已過", "最后班次已过", "final bus has departed", "last bus has departed"])


def explicitly_final(row):
    if has_departed(row):
        return False
    return any(key in remark(row) for key in ["尾班車", "尾班车", "最後班次", "最后班次", "last bus", "final bus"])


def matching(payload, sequence):
    return [row for row in payload.get("data", [])
            if row.get("route") == "72" and row.get("dir") == "I"
            and str(row.get("service_type")) == "1" and str(row.get("seq")) == str(sequence)]


def fresh(payload, now, rows):
    stamp = parse_time(payload.get("generated_timestamp"))
    if not stamp or not -60 <= (now - stamp).total_seconds() <= 180:
        return False
    return bool(rows) and all(
        (stamp := parse_time(row.get("data_timestamp"))) is not None
        and -60 <= (now - stamp).total_seconds() <= 180 for row in rows)


def choose_last_bus(origin, stop, sequence, now, previous):
    """Return a supported final-bus estimate, never infer it from one ETA alone."""
    origin_rows, stop_rows = matching(origin, 1), matching(stop, sequence)
    if not fresh(stop, now, stop_rows):
        return None, {}, "stale_or_missing_stop_data"
    valid = {}
    explicit = {}
    for row in stop_rows:
        eta = parse_time(row.get("eta"))
        if eta and 0 < (eta - now).total_seconds() < 4 * 3600:
            valid[eta] = row
            if explicitly_final(row):
                explicit[eta] = row
    if len(explicit) == 1:
        eta = next(iter(explicit))
        return {"eta": eta, "evidence": "explicit_final_bus_remark"}, {}, "confirmed"
    if len(explicit) > 1:
        return None, {}, "ambiguous_final_bus_labels"
    if not valid and any(has_departed(row) for row in stop_rows):
        return None, {}, "final_bus_already_departed"
    if len(valid) == 1 and not fresh(origin, now, origin_rows):
        return None, {}, "stale_or_missing_origin_data"
    if len(valid) != 1 or not fresh(origin, now, origin_rows) or not any(has_departed(row) for row in origin_rows):
        return None, {}, "final_bus_not_confirmed"
    # Inference: the origin explicitly reports the final departure, and this stop
    # has one remaining ETA. Require two distinct, fresh source snapshots.
    eta = next(iter(valid))
    stamp = stop_rows[0].get("data_timestamp")
    old_eta = parse_time(previous.get("eta"))
    consistent = (old_eta is not None and abs((eta - old_eta).total_seconds()) <= 300
                  and 0 <= now.timestamp() - previous.get("checked_at", 0) <= 180)
    count = previous.get("count", 1) if consistent else 0
    old_stamp, new_stamp = parse_time(previous.get("source_timestamp")), parse_time(stamp)
    if not consistent or (new_stamp and old_stamp and new_stamp > old_stamp):
        count += 1
    candidate = {"eta": eta.isoformat(), "source_timestamp": stamp,
                 "checked_at": now.timestamp(), "count": count}
    if count >= 2:
        return {"eta": eta, "evidence": "origin_final_departed_and_two_single_eta_snapshots"}, candidate, "confirmed_by_cross_check"
    return None, candidate, "awaiting_second_snapshot"


def due(estimate, now, lead_minutes=12):
    return estimate is not None and 0 < (estimate["eta"] - now).total_seconds() <= lead_minutes * 60


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


class Monitor:
    def __init__(self, state_dir=STATE):
        self.state_dir = Path(state_dir)
        self.config = json.loads((self.state_dir / "config.json").read_text())
        self.bridge_config, self.owner, self.telegram_dir = None, None, None
        if self.delivery_mode == "telegram":
            self._load_telegram_config()
        p = self.state_dir / "progress.json"
        self.progress = json.loads(p.read_text()) if p.exists() else {}
        self.status = {"pid": os.getpid(), "enabled": self.config.get("enabled", True)}
        self.session = self.telegram = None
        self.sequence = self.progress.get("boarding_sequence", 29)

    @property
    def delivery_mode(self):
        mode = self.config.get("delivery_mode", "telegram")
        if mode not in {"telegram", "local_audio"}:
            raise ValueError("delivery_mode must be telegram or local_audio")
        return mode

    def _load_telegram_config(self):
        self.bridge_config = json.loads(Path(self.config["bridge_config"]).read_text())
        self.owner = str(self.bridge_config["owner_id"])
        self.telegram_dir = Path(self.bridge_config["telegram_state_dir"])

    def configure_delivery(self):
        if self.delivery_mode == "local_audio":
            self.telegram = None
            return
        self._load_telegram_config()
        self.telegram = Telegram(self.token(), self.session)

    def save(self):
        write_json(self.state_dir / "progress.json", self.progress)

    def allowed(self):
        if self.delivery_mode == "local_audio":
            return True
        try:
            access = json.loads((self.telegram_dir / "access.json").read_text())
        except (OSError, ValueError):
            return False
        return permitted({"from": {"id": self.owner}, "chat": {"id": self.owner, "type": "private"}}, access, self.owner)

    def token(self):
        if self.delivery_mode != "telegram":
            raise RuntimeError("Local audio delivery does not use Telegram")
        text = (self.telegram_dir / ".env").read_text()
        match = re.search(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*)$", text, re.M)
        if not match:
            raise RuntimeError("Telegram token missing")
        return match[1].strip().strip("\"'")

    async def fetch(self, path):
        async with self.session.get(BASE + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                                    timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            return await response.json()

    async def validate_route(self):
        data = await self.fetch("/route-stop/72/inbound/1")
        stops = {row["stop"]: int(row["seq"]) for row in data["data"]}
        origin, boarding, destination = [self.config[key] for key in ["origin_stop", "boarding_stop", "destination_stop"]]
        if stops.get(origin) != 1 or not (0 < stops.get(boarding, 0) < stops.get(destination, 0)):
            raise RuntimeError("Route changed; configured stops are not in the expected order")
        self.sequence = stops[boarding]
        self.progress["boarding_sequence"] = self.sequence

    async def prepare_voice(self, path, text):
        if path.exists() and path.stat().st_size > 1000:
            return
        tmp = path.with_suffix(".tmp.mp3")
        for host in ["translate.google.com", "translate.google.com.hk"]:
            try:
                async with self.session.get(f"https://{host}/translate_tts",
                    params={"ie": "UTF-8", "client": "tw-ob", "tl": self.config.get("speech_language", "zh-CN"), "q": text},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=20)) as response:
                    response.raise_for_status()
                    data = await response.read()
                    if not response.headers.get("Content-Type", "").startswith("audio/") or len(data) < 1000:
                        raise RuntimeError("Speech synthesis returned an empty file")
                    tmp.write_bytes(data)
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                if host == "translate.google.com.hk":
                    raise RuntimeError("Speech synthesis unavailable") from None
        tmp.replace(path)

    def alert_clip(self):
        if self.delivery_mode == "local_audio":
            return self.state_dir / "alert-local.mp3", LOCAL_ALERT_VOICE
        return self.state_dir / "alert.mp3", ALERT_VOICE

    async def prepare_alert(self):
        path, text = self.alert_clip()
        await self.prepare_voice(path, text)
        return path

    async def send_voice(self, path, caption):
        if self.delivery_mode != "telegram":
            raise RuntimeError("Telegram voice is disabled for local audio delivery")
        if not self.allowed():
            raise RuntimeError("Recipient is no longer allowlisted")
        with path.open("rb") as media:
            form = aiohttp.FormData()
            form.add_field("chat_id", self.owner)
            form.add_field("caption", caption)
            form.add_field("voice", media, filename=path.name, content_type="audio/mpeg")
            try:
                async with self.session.post(f"https://api.telegram.org/bot{self.telegram.token}/sendVoice",
                                             data=form, timeout=aiohttp.ClientTimeout(total=45)) as response:
                    data = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise ConnectionError("Telegram sendVoice: " + type(error).__name__) from None
        if not data.get("ok"):
            raise TelegramError("sendVoice", data.get("error_code", 0),
                                data.get("description", "Error").replace(self.telegram.token, "[redacted]"))
        return data["result"]

    async def check(self, now=None, send=True):
        now = now or datetime.now(HK)
        origin, stop = await asyncio.gather(
            self.fetch(f"/eta/{self.config['origin_stop']}/72/1"),
            self.fetch(f"/eta/{self.config['boarding_stop']}/72/1"))
        day = operating_day(now)
        previous = self.progress.get("candidate", {}) if self.progress.get("candidate_day") == day else {}
        estimate, candidate, reason = choose_last_bus(origin, stop, self.sequence, now, previous)
        self.progress.update(candidate_day=day, candidate=candidate)
        self.save()
        self.status.update(last_checked_at=now.isoformat(), operating_day=day, reason=reason,
                           eta=estimate["eta"].isoformat() if estimate else None,
                           evidence=estimate["evidence"] if estimate else None,
                           source_timestamp=stop.get("generated_timestamp"), last_error=None)
        if reason.startswith("stale_or_missing_"):
            raise RuntimeError("Live ETA data unavailable")
        if send and due(estimate, now, self.config["lead_minutes"]) and self.progress.get("alerted_day") != day:
            await self.deliver(estimate, now, day)
        return self.status

    async def deliver(self, estimate, now, day):
        if self.delivery_mode == "local_audio":
            voice_path = await self.prepare_alert()
            await play_audio(voice_path, self.config.get("audio_device", "default"))
            self.progress.update(alerted_day=day, message_id=None, mode="local_audio",
                                 alerted_at=datetime.now(HK).isoformat(), alerted_eta=estimate["eta"].isoformat())
            self.save()
            LOG.info("Reminder played locally: day=%s", day)
            return
        if not self.allowed():
            raise RuntimeError("Recipient is no longer allowlisted")
        minutes = max(1, math.ceil((estimate["eta"] - now).total_seconds() / 60))
        caption = (f"九巴72末班车提醒｜赤泥坪 → 广福道（往太和）\n"
                   f"预计 {estimate['eta'].astimezone(HK):%H:%M} 到赤泥坪，约剩 {minutes} 分钟，请准备出发。\n"
                   f"查询时间：{now:%H:%M:%S}（香港时间）；到站时间可能变化。")
        try:
            voice_path = await self.prepare_alert()
            sent = await self.send_voice(voice_path, caption)
            message_id, mode = sent["message_id"], "voice"
        except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError, TelegramError, RuntimeError) as error:
            # Text remains useful when speech synthesis or media upload is unavailable.
            LOG.warning("Voice unavailable: %s; trying text", type(error).__name__)
            if not self.allowed():
                raise RuntimeError("Recipient is no longer allowlisted")
            sent = await self.telegram.call("sendMessage", chat_id=self.owner, text=caption + "\n语音暂不可用，此次先以文字提醒。")
            message_id, mode = sent["message_id"], "text_fallback"
        self.progress.update(alerted_day=day, message_id=message_id, mode=mode,
                             alerted_at=datetime.now(HK).isoformat(), alerted_eta=estimate["eta"].isoformat())
        self.save()
        LOG.info("Reminder delivered: day=%s message=%s mode=%s", day, message_id, mode)

    def health(self):
        self.status.update(updated_at=datetime.now(HK).isoformat(),
                           delivery_mode=self.delivery_mode, mode=self.progress.get("mode"),
                           audio_device=self.config.get("audio_device", "default") if self.delivery_mode == "local_audio" else None,
                           last_delivery_mode=self.progress.get("mode"), **{
            key: self.progress.get(key) for key in ["alerted_day", "alerted_at", "message_id"]})
        write_json(self.state_dir / "health.json", self.status)

    async def test_voice(self):
        path = self.state_dir / "test.mp3"
        await self.prepare_voice(path, TEST_VOICE)
        if self.delivery_mode == "local_audio":
            await play_audio(path, self.config.get("audio_device", "default"))
            return {"delivery_mode": "local_audio", "test_audio_played": True}
        self.configure_delivery()
        sent = await self.send_voice(path, "语音测试，不是实际到站提醒。规则：每天香港时间午夜前后检查，末班车预计到赤泥坪前约12分钟提醒一次。")
        return {"test_voice_message_id": sent["message_id"], "has_voice": "voice" in sent}

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.configure_delivery()
            # Cache audio before the nightly window so the reminder needs no live TTS.
            await self.prepare_alert()
            start, end = self.config["start_minute"], self.config["end_minute"]
            LOG.info("Enabled KMB72 monitor: Asia/Hong_Kong, %02d:%02d-%02d:%02d, lead=%s minutes",
                     start // 60, start % 60, end // 60, end % 60, self.config["lead_minutes"])
            while True:
                now = datetime.now(HK)
                try:
                    self.config = json.loads((self.state_dir / "config.json").read_text())
                    self.configure_delivery()
                    active = self.config.get("enabled", True) and in_window(now, self.config)
                    start, end = self.config["start_minute"], self.config["end_minute"]
                    self.status.update(enabled=self.config.get("enabled", True), active_window=active,
                        check_window=f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}",
                        timezone="Asia/Hong_Kong")
                    if active and self.allowed() and self.progress.get("alerted_day") != operating_day(now):
                        if self.progress.get("validated_day") != operating_day(now):
                            await self.validate_route()
                            self.progress["validated_day"] = operating_day(now)
                            self.save()
                        await self.check(now)
                        self.progress["failures"] = 0
                        self.save()
                except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, TelegramError, RuntimeError, ValueError, KeyError) as error:
                    self.status["last_error"] = type(error).__name__
                    self.progress["failures"] = self.progress.get("failures", 0) + 1
                    LOG.warning("Monitor check failed: %s", type(error).__name__)
                    # A real data outage must not look like successful monitoring.
                    day = operating_day(now)
                    if (self.delivery_mode == "telegram" and self.progress["failures"] >= 3
                            and self.progress.get("error_notice_day") != day and self.allowed()):
                        try:
                            await self.telegram.call("sendMessage", chat_id=self.owner,
                                text="九巴72提醒：实时数据或通知接口连续失败，今晚可能无法及时提醒。请同时查看 App1933 的赤泥坪往太和方向到站时间。")
                            self.progress["error_notice_day"] = day
                        except (TelegramError, ConnectionError):
                            pass
                    self.save()
                self.health()
                await asyncio.sleep(self.config.get("interval_seconds", 60))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "status", "check", "test-voice", "prepare"])
    parser.add_argument("--state-dir", type=Path, default=STATE)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    monitor = Monitor(args.state_dir)
    if args.action == "status":
        print((args.state_dir / "health.json").read_text())
        return
    lock = None
    if args.action == "run":
        lock = (args.state_dir / "monitor.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Bus reminder is already running")

    async def task():
        if args.action == "run":
            await monitor.run()
            return
        async with aiohttp.ClientSession() as session:
            monitor.session = session
            if args.action == "check":
                await monitor.validate_route()
                # Dry-run: inspect current estimates; never send a travel alert here.
                print(json.dumps(await monitor.check(send=False), ensure_ascii=False, indent=2))
            elif args.action == "prepare":
                await monitor.prepare_alert()
                print("Cached alert voice")
            else:
                print(json.dumps(await monitor.test_voice()))

    async def serve():
        current = asyncio.create_task(task())
        loop = asyncio.get_running_loop()
        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
            loop.add_signal_handler(sig, current.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await current

    asyncio.run(serve())


if __name__ == "__main__":
    main()
