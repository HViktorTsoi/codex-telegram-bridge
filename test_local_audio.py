"""Local playback keeps the decoder, cache and child process under control."""
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from local_audio import LocalAudioError, _decode, _prepare_wav, play_audio


def wav(path, frames=240):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\0\0' * frames)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.source = self.path / 'alert.mp3'
        self.source.write_bytes(b'cached MP3 input')

    def tearDown(self):
        self.tmp.cleanup()

    def decode(self, source, target):
        self.assertEqual(source, self.source)
        self.assertNotEqual(source, target)
        wav(target)

    def test_conversion_is_cached_as_a_valid_wav_and_never_changes_the_mp3(self):
        with patch('local_audio._decode', side_effect=self.decode) as decode:
            target, duration = _prepare_wav(self.source)
            self.assertEqual((target, duration), (self.source.with_suffix('.wav'), 0.01))
            self.assertEqual(_prepare_wav(self.source), (target, duration))
        decode.assert_called_once()
        self.assertEqual(self.source.read_bytes(), b'cached MP3 input')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_modified_source_replaces_the_cache(self):
        target = self.source.with_suffix('.wav')
        wav(target, 10)
        os.utime(target, ns=(1, 1))
        with patch('local_audio._decode', side_effect=self.decode) as decode:
            _, duration = _prepare_wav(self.source)
        decode.assert_called_once()
        self.assertEqual(duration, 0.01)

    def test_invalid_and_truncated_cache_are_rebuilt(self):
        target = self.source.with_suffix('.wav')
        for contents in (b'', b'not a wav'):
            with self.subTest(contents=contents):
                target.write_bytes(contents)
                with patch('local_audio._decode', side_effect=self.decode) as decode:
                    _prepare_wav(self.source)
                decode.assert_called_once()
        wav(target)
        target.write_bytes(target.read_bytes()[:-10])
        with patch('local_audio._decode', side_effect=self.decode) as decode:
            _prepare_wav(self.source)
        decode.assert_called_once()

    def test_failed_conversion_preserves_previous_cache_and_cleans_temporary_file(self):
        target = self.source.with_suffix('.wav')
        wav(target, 10)
        before = target.read_bytes()
        os.utime(target, ns=(1, 1))
        with patch('local_audio._decode', side_effect=LocalAudioError('decode failed')):
            with self.assertRaises(LocalAudioError):
                _prepare_wav(self.source)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_real_wav_can_be_played_without_a_decoder(self):
        source = self.path / 'test.wav'
        wav(source)
        with patch('local_audio._decode') as decode:
            target, duration = _prepare_wav(source)
        self.assertEqual((target, duration), (source, 0.01))
        decode.assert_not_called()

    def test_source_replaced_during_conversion_never_installs_a_stale_cache(self):
        target = self.source.with_suffix('.wav')
        wav(target, 10)
        before = target.read_bytes()
        os.utime(target, ns=(1, 1))

        def changed(source, temporary):
            self.decode(source, temporary)
            source.write_bytes(b'new speech data replacing the MP3')

        with patch('local_audio._decode', side_effect=changed):
            with self.assertRaisesRegex(LocalAudioError, 'changed while decoding'):
                _prepare_wav(self.source)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_decoder_open_read_end_of_file_and_close_failures_are_detected(self):
        def opened(path, mode, info):
            info._obj.samplerate, info._obj.channels = 24000, 1
            return 1

        for name, open_result, reads, error, closed in (
                ('open', None, [], 0, 0),
                ('negative-read', opened, [-1], 0, 0),
                ('decoder-error', opened, [0], 1, 0),
                ('empty', opened, [0], 0, 0),
                ('close', opened, [2, 0], 0, 1)):
            with self.subTest(name=name):
                library = SimpleNamespace(
                    sf_open=Mock(side_effect=open_result) if callable(open_result) else Mock(return_value=open_result),
                    sf_readf_short=Mock(side_effect=reads), sf_error=Mock(return_value=error),
                    sf_close=Mock(return_value=closed))
                with patch('local_audio._sndfile', return_value=library), self.assertRaises(LocalAudioError):
                    _decode(self.source, self.path / 'temporary.wav')
                self.assertEqual(library.sf_close.call_count, 0 if name == 'open' else 1)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    def process(self, code=0, stderr=b''):
        process = Mock(returncode=code)
        process.communicate = AsyncMock(return_value=(b'', stderr))
        process.wait = AsyncMock(return_value=code)
        return process

    async def test_player_uses_safe_argv_and_waits_for_completion(self):
        process = self.process()
        device = 'default:CARD=Generic;not-a-shell-command'
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 17.7)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as start:
            await play_audio('/cache/alert.mp3', device)
        self.assertEqual(start.call_args.args, ('/usr/bin/aplay', '-q', '-D', device, '/cache/alert.wav'))
        self.assertEqual(start.call_args.kwargs,
                         {'stdout': asyncio.subprocess.DEVNULL, 'stderr': asyncio.subprocess.PIPE})
        process.communicate.assert_awaited_once()
        process.terminate.assert_not_called()

    async def test_nonzero_exit_is_not_reported_as_success(self):
        process = self.process(1, b'cannot open output device')
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 1)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaisesRegex(LocalAudioError, 'cannot open output device'):
                await play_audio('/cache/alert.mp3')

    async def test_timeout_terminates_and_reaps_player(self):
        process = self.process()
        process.returncode = None
        process.communicate.side_effect = asyncio.TimeoutError()
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 1)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaisesRegex(LocalAudioError, 'timed out'):
                await play_audio('/cache/alert.mp3')
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_cancellation_stops_and_reaps_player(self):
        process, entered = self.process(), asyncio.Event()
        process.returncode = None

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        process.communicate.side_effect = blocked
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 1)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(play_audio('/cache/alert.mp3'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_unresponsive_player_is_killed_and_reaped_after_timeout(self):
        process = self.process()
        process.returncode = None
        process.communicate.side_effect = asyncio.TimeoutError()
        process.wait.side_effect = [asyncio.TimeoutError(), 0]
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 1)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(LocalAudioError):
                await play_audio('/cache/alert.mp3')
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    async def test_invalid_device_and_missing_player_fail_before_claiming_playback(self):
        for device in ('', '\0', None):
            with self.subTest(device=device), self.assertRaises(LocalAudioError):
                await play_audio('/cache/alert.mp3', device)
        with patch('local_audio._prepare_wav', return_value=(Path('/cache/alert.wav'), 1)), \
                patch('local_audio.asyncio.create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError())):
            with self.assertRaisesRegex(LocalAudioError, 'Cannot start'):
                await play_audio('/cache/alert.mp3')


if __name__ == '__main__':
    unittest.main()
