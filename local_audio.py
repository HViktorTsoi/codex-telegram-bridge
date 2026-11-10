"""Decode cached speech and play it through the local ALSA output."""
import array
import asyncio
import ctypes
import ctypes.util
import os
from pathlib import Path
import sys
import tempfile
import wave


class LocalAudioError(RuntimeError):
    pass


class _SoundInfo(ctypes.Structure):
    _fields_ = [('frames', ctypes.c_longlong), ('samplerate', ctypes.c_int),
                ('channels', ctypes.c_int), ('format', ctypes.c_int),
                ('sections', ctypes.c_int), ('seekable', ctypes.c_int)]


def _wav_duration(path):
    with wave.open(str(path), 'rb') as audio:
        frames, rate, channels = audio.getnframes(), audio.getframerate(), audio.getnchannels()
        if (frames <= 0 or rate <= 0 or not 1 <= channels <= 32
                or audio.getsampwidth() != 2 or audio.getcomptype() != 'NONE'):
            raise LocalAudioError('Cached speech is not a valid PCM WAV file')
        audio.setpos(frames - 1)
        if len(audio.readframes(1)) != channels * 2:
            raise LocalAudioError('Cached speech WAV is truncated')
        return frames / rate


def _signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _sndfile():
    name = ctypes.util.find_library('sndfile')
    if not name:
        raise LocalAudioError('Local speech decoding requires libsndfile')
    library = ctypes.CDLL(name)
    library.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_SoundInfo)]
    library.sf_open.restype = ctypes.c_void_p
    library.sf_readf_short.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_short), ctypes.c_longlong]
    library.sf_readf_short.restype = ctypes.c_longlong
    library.sf_error.argtypes = [ctypes.c_void_p]
    library.sf_error.restype = ctypes.c_int
    library.sf_close.argtypes = [ctypes.c_void_p]
    library.sf_close.restype = ctypes.c_int
    return library


def _decode(source, target):
    library, info = _sndfile(), _SoundInfo()
    handle = library.sf_open(os.fsencode(source), 0x10, ctypes.byref(info))
    if not handle:
        raise LocalAudioError('Cannot decode the cached speech file')
    try:
        if info.samplerate <= 0 or not 1 <= info.channels <= 32:
            raise LocalAudioError('Speech decoder returned invalid audio parameters')
        chunk = 32768
        buffer = (ctypes.c_short * (chunk * info.channels))()
        total = 0
        with wave.open(str(target), 'wb') as audio:
            audio.setnchannels(info.channels)
            audio.setsampwidth(2)
            audio.setframerate(info.samplerate)
            while True:
                count = library.sf_readf_short(handle, buffer, chunk)
                if not 0 <= count <= chunk:
                    raise LocalAudioError('Speech decoder failed while reading audio')
                if count == 0:
                    if library.sf_error(handle):
                        raise LocalAudioError('Speech decoder failed before end of audio')
                    break
                data = ctypes.string_at(buffer, count * info.channels * 2)
                if sys.byteorder != 'little':
                    samples = array.array('h', data)
                    samples.byteswap()
                    data = samples.tobytes()
                audio.writeframesraw(data)
                total += count
        if total == 0:
            raise LocalAudioError('Cached speech contains no audio frames')
    except BaseException:
        library.sf_close(handle)
        raise
    if library.sf_close(handle) != 0:
        raise LocalAudioError('Speech decoder could not close the audio file')


def _prepare_wav(path):
    try:
        source = Path(path).expanduser().absolute()
        signature = _signature(source)
        if not source.is_file() or signature[2] == 0:
            raise LocalAudioError('Cached speech file is missing or empty')
        if source.suffix.lower() == '.wav':
            return source, _wav_duration(source)
        target = source.with_suffix('.wav')
        if target.exists() and target.stat().st_mtime_ns >= signature[3]:
            try:
                return target, _wav_duration(target)
            except (OSError, EOFError, wave.Error, LocalAudioError):
                pass
        fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
        os.close(fd)
        temporary = Path(temporary)
        try:
            _decode(source, temporary)
            duration = _wav_duration(temporary)
            if _signature(source) != signature:
                raise LocalAudioError('Cached speech changed while decoding')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, duration
    except LocalAudioError:
        raise
    except (OSError, EOFError, wave.Error, TypeError, ValueError) as error:
        raise LocalAudioError('Cannot prepare cached speech for local playback') from error


async def _stop_player(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def play_audio(path, device='default'):
    """Return only after aplay has successfully finished; never alter volume."""
    if not isinstance(device, str) or not device.strip() or '\x00' in device:
        raise LocalAudioError('Invalid local audio output device')
    target, duration = await asyncio.to_thread(_prepare_wav, path)
    try:
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/aplay', '-q', '-D', device, str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as error:
        raise LocalAudioError('Cannot start the local audio player') from error
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), max(30.0, duration + 15.0))
    except asyncio.CancelledError:
        await asyncio.shield(_stop_player(process))
        raise
    except asyncio.TimeoutError as error:
        await asyncio.shield(_stop_player(process))
        raise LocalAudioError('Local speech playback timed out') from error
    except Exception as error:
        await asyncio.shield(_stop_player(process))
        raise LocalAudioError('Cannot finish local speech playback') from error
    if process.returncode != 0:
        detail = (stderr or b'').decode('utf-8', errors='replace').strip()[:500]
        raise LocalAudioError('Local audio player failed' + (': ' + detail if detail else ''))
