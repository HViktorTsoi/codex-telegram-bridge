"""Read the local network name without credentials or quota requests."""
import asyncio
import ctypes
import fcntl
from pathlib import Path
import socket
import time


UNKNOWN = {'type': 'unknown'}


def _clean(value):
    if not isinstance(value, dict):
        return dict(UNKNOWN)
    kind = value.get('type')
    if kind == 'wifi' and isinstance(value.get('ssid'), str):
        ssid = ''.join(c if c.isprintable() else ' ' for c in value['ssid']).strip()[:96]
        return {'type': 'wifi', 'ssid': ssid} if ssid else dict(UNKNOWN)
    return {'type': kind} if kind in ('wired', 'disconnected', 'unknown') else dict(UNKNOWN)


def _wireless_ssid(ifname):
    """SIOCGIWESSID reads only the ESSID of an associated local interface."""
    class Point(ctypes.Structure):
        _fields_ = [('pointer', ctypes.c_void_p), ('length', ctypes.c_uint16), ('flags', ctypes.c_uint16)]

    class Data(ctypes.Union):
        _fields_ = [('essid', Point), ('padding', ctypes.c_byte * 16)]

    class Request(ctypes.Structure):
        _fields_ = [('ifname', ctypes.c_char * 16), ('data', Data)]

    encoded = ifname.encode()
    if not encoded or len(encoded) >= 16:
        return None
    buffer = ctypes.create_string_buffer(33)
    request = Request()
    request.ifname = encoded
    request.data.essid.pointer = ctypes.addressof(buffer)
    request.data.essid.length = len(buffer)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            fcntl.ioctl(sock.fileno(), 0x8B1B, request)
    except OSError:
        return None
    size = min(request.data.essid.length, 32)
    if not request.data.essid.flags or not size:
        return None
    return buffer.raw[:size].rstrip(b'\0').decode('utf-8', errors='replace') or None


def _linux_network(sys_net=Path('/sys/class/net'), read_ssid=_wireless_ssid):
    """Require physical-device evidence; a container eth0 is not host Wi-Fi."""
    try:
        interfaces = list(sys_net.iterdir())
    except OSError:
        return dict(UNKNOWN)
    ssids, physical_states, wired_up, wireless_unknown = [], [], False, False
    for interface in interfaces:
        wireless = (interface / 'wireless').exists() or (interface / 'phy80211').exists()
        physical = wireless or (interface / 'device').exists()
        if not physical:
            continue
        try:
            state = (interface / 'operstate').read_text().strip()
        except OSError:
            physical_states.append('unknown')
            continue
        physical_states.append(state)
        if wireless and state == 'up':
            ssid = read_ssid(interface.name)
            if ssid:
                ssids.append(ssid)
            else:
                wireless_unknown = True
        elif not wireless and state == 'up':
            try:
                wired_up = wired_up or ((interface / 'type').read_text().strip() == '1'
                                        and (interface / 'carrier').read_text().strip() == '1')
            except OSError:
                pass
    if ssids:
        return _clean({'type': 'wifi', 'ssid': ' / '.join(dict.fromkeys(ssids))})
    if wireless_unknown:
        return dict(UNKNOWN)
    if wired_up:
        return {'type': 'wired'}
    if physical_states and all(state in ('down', 'dormant', 'notpresent', 'lowerlayerdown')
                               for state in physical_states):
        return {'type': 'disconnected'}
    return dict(UNKNOWN)


def _fields(line):
    values, part, escaped = [], [], False
    for char in line:
        if escaped:
            part.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            values.append(''.join(part))
            part = []
        else:
            part.append(char)
    if escaped:
        part.append('\\')
    return values + [''.join(part)]


async def _command(*args):
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (FileNotFoundError, PermissionError):
        return None
    try:
        output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return output[:65536].decode('utf-8', errors='replace') if process.returncode == 0 else None


async def collect_network():
    value = await asyncio.to_thread(_linux_network)
    if value['type'] != 'unknown':
        return value
    status = await _command('nmcli', '-t', '--escape', 'yes', '-f', 'TYPE,STATE', 'device', 'status')
    if status is not None:
        devices = [_fields(line) for line in status.splitlines()]
        physical = [row for row in devices if len(row) == 2 and row[0] in ('wifi', 'ethernet')]
        if any(kind == 'wifi' and state.startswith('connected') for kind, state in physical):
            access_points = await _command('nmcli', '-t', '--escape', 'yes', '-f', 'IN-USE,SSID',
                                           'device', 'wifi', 'list', '--rescan', 'no')
            active = [_fields(line) for line in (access_points or '').splitlines()]
            ssids = [row[1] for row in active if len(row) == 2 and row[0] == '*' and row[1]]
            if ssids:
                return _clean({'type': 'wifi', 'ssid': ' / '.join(dict.fromkeys(ssids))})
        elif any(kind == 'ethernet' and state.startswith('connected') for kind, state in physical):
            return {'type': 'wired'}
        elif physical and all(state in ('disconnected', 'unavailable') for _, state in physical):
            return {'type': 'disconnected'}
    ssid = await _command('iwgetid', '--raw')
    return _clean({'type': 'wifi', 'ssid': ssid.rstrip('\n')}) if ssid else dict(UNKNOWN)


class HostNetwork:
    """Background local collection, independent of the account quota cache."""
    def __init__(self, provider=None, clock=time.time, cache_seconds=60, timeout=3):
        self.provider, self.clock = provider or collect_network, clock
        self.cache_seconds, self.timeout = cache_seconds, timeout
        self.value, self.next_refresh, self.task = dict(UNKNOWN), float('-inf'), None
        self.lock = asyncio.Lock()

    def label(self):
        if self.value['type'] == 'wifi':
            return 'Wi-Fi ' + self.value['ssid']
        return {'wired': '有线网络', 'disconnected': '网络未连接', 'unknown': '网络未知'}[self.value['type']]

    def poll(self):
        if self.clock() >= self.next_refresh and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.refresh())

    async def refresh(self):
        if self.lock.locked() or self.clock() < self.next_refresh:
            return False
        async with self.lock:
            self.next_refresh = self.clock() + self.cache_seconds
            try:
                value = await asyncio.wait_for(self.provider(), self.timeout)
            except Exception:
                value = UNKNOWN
            self.value = _clean(value)
            return True

    async def aclose(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
