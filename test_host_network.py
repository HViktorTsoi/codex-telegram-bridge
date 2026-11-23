"""Network display reads local evidence and never blocks the quota UI."""
import asyncio
import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from host_network import HostNetwork, _linux_network, _wireless_ssid, collect_network


class LinuxNetworkTests(unittest.TestCase):
    def test_wireless_ioctl_reads_only_confirmed_ssid_bytes(self):
        def ioctl(fd, operation, request):
            self.assertEqual(operation, 0x8B1B)
            self.assertEqual(request.ifname, b'wlan0')
            data = 'Wi-Fi：研究'.encode()
            ctypes.memmove(request.data.essid.pointer, data, len(data))
            request.data.essid.length = len(data)
            request.data.essid.flags = 1
        with patch('host_network.fcntl.ioctl', side_effect=ioctl):
            self.assertEqual(_wireless_ssid('wlan0'), 'Wi-Fi：研究')

    def test_wireless_ioctl_rejects_unassociated_and_invalid_interfaces(self):
        def disconnected(fd, operation, request):
            ctypes.memmove(request.data.essid.pointer, b'old-ssid', 8)
            request.data.essid.length = 8
            request.data.essid.flags = 0
        with patch('host_network.fcntl.ioctl', side_effect=disconnected) as ioctl:
            self.assertIsNone(_wireless_ssid('wlan0'))
            self.assertIsNone(_wireless_ssid('x' * 16))
            self.assertEqual(ioctl.call_count, 1)
        with patch('host_network.fcntl.ioctl', side_effect=OSError()):
            self.assertIsNone(_wireless_ssid('wlan0'))

    @staticmethod
    def interface(root, name, *, wireless=False, physical=True, state='up'):
        p = root / name
        p.mkdir()
        if wireless:
            (p / 'wireless').mkdir()
        elif physical:
            (p / 'device').mkdir()
        (p / 'operstate').write_text(state)
        (p / 'type').write_text('1')
        (p / 'carrier').write_text('1' if state == 'up' else '0')
        return p

    def test_active_wireless_has_ssid_without_external_commands(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.interface(root, 'wlan0', wireless=True)
            self.interface(root, 'enp1s0')
            self.assertEqual(_linux_network(root, lambda _: 'CUHK1x'),
                             {'type': 'wifi', 'ssid': 'CUHK1x'})

    def test_container_eth0_never_establishes_host_wifi_or_wired_status(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.interface(root, 'eth0', physical=False)
            self.assertEqual(_linux_network(root), {'type': 'unknown'})

    def test_physical_wired_and_disconnected_are_distinct(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            p = self.interface(root, 'enp1s0')
            self.assertEqual(_linux_network(root), {'type': 'wired'})
            (p / 'operstate').write_text('down')
            self.assertEqual(_linux_network(root), {'type': 'disconnected'})

    def test_unknown_active_wifi_is_not_reported_as_wired(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.interface(root, 'wlan0', wireless=True)
            self.interface(root, 'enp1s0')
            self.assertEqual(_linux_network(root, lambda _: None), {'type': 'unknown'})


class HostNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_sysfs_collection_skips_external_tools(self):
        with patch('host_network._linux_network', return_value={'type': 'wifi', 'ssid': 'local'}), \
                patch('host_network._command', new_callable=AsyncMock) as command:
            self.assertEqual(await collect_network(), {'type': 'wifi', 'ssid': 'local'})
            command.assert_not_awaited()

    async def test_nmcli_fallback_reads_active_ssid_not_connection_profile_name(self):
        with patch('host_network._linux_network', return_value={'type': 'unknown'}), \
                patch('host_network._command', new_callable=AsyncMock,
                      side_effect=['wifi:connected\nethernet:disconnected\n',
                                   '*:Research\\:Lab\\\\WiFi\n:unrelated\n']) as command:
            self.assertEqual(await collect_network(), {'type': 'wifi', 'ssid': 'Research:Lab\\WiFi'})
            self.assertIn('--rescan', command.await_args_list[1].args)

    async def test_nmcli_known_non_wifi_states_and_missing_tools(self):
        for status, expected in [('wifi:disconnected\nethernet:connected\n', 'wired'),
                                 ('wifi:disconnected\nethernet:unavailable\n', 'disconnected')]:
            with self.subTest(status=status), \
                    patch('host_network._linux_network', return_value={'type': 'unknown'}), \
                    patch('host_network._command', new_callable=AsyncMock, return_value=status):
                self.assertEqual(await collect_network(), {'type': expected})
        with patch('host_network._linux_network', return_value={'type': 'unknown'}), \
                patch('host_network._command', new_callable=AsyncMock, side_effect=[None, None]):
            self.assertEqual(await collect_network(), {'type': 'unknown'})

    async def test_iwgetid_fallback_and_ssid_display_sanitization(self):
        with patch('host_network._linux_network', return_value={'type': 'unknown'}), \
                patch('host_network._command', new_callable=AsyncMock, side_effect=[None, 'host-wifi\n']):
            self.assertEqual(await collect_network(), {'type': 'wifi', 'ssid': 'host-wifi'})
        network = HostNetwork(provider=AsyncMock(return_value={'type': 'wifi', 'ssid': 'SSID\nInjected\x00'}))
        await network.refresh()
        self.assertEqual(network.label(), 'Wi-Fi SSID Injected')

    async def test_sixty_second_cache_and_concurrent_reads_coalesce(self):
        now = [1000]
        entered, release = asyncio.Event(), asyncio.Event()
        async def provider():
            entered.set()
            await release.wait()
            return {'type': 'wifi', 'ssid': 'one'}
        provider = AsyncMock(side_effect=provider)
        network = HostNetwork(provider=provider, clock=lambda: now[0])
        network.poll()
        await entered.wait()
        for _ in range(5): network.poll()
        self.assertFalse(await network.refresh())
        release.set()
        await network.task
        self.assertEqual(network.label(), 'Wi-Fi one')
        now[0] += 59
        self.assertFalse(await network.refresh())
        self.assertEqual(provider.await_count, 1)
        now[0] += 1
        self.assertTrue(await network.refresh())
        self.assertEqual(provider.await_count, 2)

    async def test_timeout_discards_stale_wifi_and_backs_off(self):
        now = [1000]
        async def slow(): await asyncio.Event().wait()
        provider = AsyncMock(return_value={'type': 'wifi', 'ssid': 'old'})
        network = HostNetwork(provider=provider, clock=lambda: now[0], timeout=.02)
        await network.refresh()
        self.assertEqual(network.label(), 'Wi-Fi old')
        now[0] += 60
        provider.side_effect = slow
        await network.refresh()
        self.assertEqual(network.label(), '网络未知')
        self.assertFalse(await network.refresh())
        self.assertEqual(provider.await_count, 2)

    async def test_background_poll_is_immediate_and_close_cancels_work(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        network = HostNetwork(provider=slow, timeout=30)
        network.poll()
        self.assertEqual(network.label(), '网络未知')
        await entered.wait()
        await network.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(network.task.done())


if __name__ == '__main__':
    unittest.main()
