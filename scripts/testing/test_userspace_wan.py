import asyncio
import importlib.util
from pathlib import Path
import statistics
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_experiment as runner
spec = importlib.util.spec_from_file_location('userspace_wan', ROOT / 'scripts/comm/userspace-wan.py')
relay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_module)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_bidirectional_stream_and_byte_accounting(self):
        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.close()
        target = await asyncio.start_server(echo, '127.0.0.1', 0)
        relay = relay_module.Relay(['127.0.0.1'])
        server = await asyncio.start_server(relay.handle, '127.0.0.1', 0)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(f'127.0.0.1\t{target.sockets[0].getsockname()[1]}\n'.encode())
            await writer.drain()
            self.assertEqual(await reader.readexactly(1), b'O')
            samples = []
            for _ in range(5):
                start = time.monotonic()
                writer.write(b'hello')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 2), b'hello')
                samples.append(time.monotonic() - start)
            self.assertGreaterEqual(statistics.median(samples), .013)
            self.assertLess(statistics.median(samples), .2)
            data = bytes(range(256)) * 1024
            writer.write(data)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(len(data)), 3), data)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.1)
            self.assertEqual(relay.stats['bytes'], 2 * (25 + len(data)))
            self.assertEqual(relay.stats['errors'], [])
        finally:
            server.close()
            target.close()
            await relay.close()
            await server.wait_closed()
            await target.wait_closed()

    async def test_rate_scheduling_is_shared_across_streams(self):
        relay = relay_module.Relay([], delay=0, rate=10000)
        class Writer:
            def write(self, data):
                pass
            async def drain(self):
                pass
            def can_write_eof(self):
                return False
        readers = [asyncio.StreamReader(), asyncio.StreamReader()]
        for reader in readers:
            reader.feed_data(b'x' * 1000)
            reader.feed_eof()
        start = time.monotonic()
        await asyncio.gather(*(relay.pipe(r, Writer(), 'same-link') for r in readers))
        self.assertGreaterEqual(time.monotonic() - start, .19)
        self.assertEqual(relay.stats['bytes'], 2000)

    async def test_reader_error_propagates_without_hanging(self):
        reader = asyncio.StreamReader()
        reader.set_exception(ConnectionResetError('fixture reset'))
        with self.assertRaises(ConnectionResetError):
            await asyncio.wait_for(relay_module.Relay([]).pipe(reader, None, 'link'), 1)


class ConfigurationTests(unittest.TestCase):
    def test_userspace_requires_distributed_3pc_and_keeps_hints(self):
        args = runner.build_parser().parse_args(['-p', '3', '-s', 'wan', '-c', 'nocopy', '--hosts',
            'zf01,zf02,zf03', '--wan-sim', 'userspace-distributed', 'q1'])
        with mock.patch.object(runner, 'discover_iface_and_subnet', return_value=(None, None)), \
                mock.patch.object(runner, '_get_git_info', return_value=(None, None, None)):
            cfg = runner.derive_config(args)
        self.assertIn('userspace-wan.py', cfg.run_cmd)
        self.assertEqual(cfg.userspace_wan['cost_model'], {'latency_ms': 6.5, 'bandwidth_gbps': 12})
        self.assertIsNone(cfg.rootless)
        args.exp_setting = 'same'
        with self.assertRaises(SystemExit):
            runner.derive_config(args)


class ReportTests(unittest.TestCase):
    def test_relay_evidence_is_parsed(self):
        import report_tpch_plain_and_3pc as report
        data = report.parse_log('[USERSPACE_WAN] {"rank":0,"bytes":123,"connections":2,"errors":[]}')
        self.assertEqual(data['userspace_relays'][0]['bytes'], 123)
        data = report.parse_log('[USERSPACE_WAN] invalid')
        self.assertTrue(data['userspace_relays'][0]['errors'])
