"""Check scaling, fault preservation, corrupt packets, and reconnect behavior."""
import importlib.machinery
import importlib.util
import queue
import struct
import threading
import unittest
from unittest.mock import patch

import protocol
import limits


def packet():
    payload = bytearray(100)
    struct.pack_into('<4hH', payload, 0, 264, 310, -1000, -50, 3300)
    payload[10] = 42
    for n in range(6):
        struct.pack_into('<h2xII', payload, 12+n*12, 12000, 8333, 99999)
    struct.pack_into('<HBBHH', payload, 92, 12000, 0, 0, 0x20, 0x8001)
    return payload


class ProtocolTests(unittest.TestCase):
    def test_configuration_versions_and_alarm_settings(self):
        for version, size in enumerate((72, 74, 96)):
            payload = bytearray(size)
            payload[2] = version
            struct.pack_into('<4HhBBH4B', payload, 46,
                             63, 63, 0, 63, 700, 55, 105, 660, 40, 6, 10, 60)
            cfg = protocol.decode_configuration(payload)
            self.assertEqual(cfg['wire_current_limit'], 10.5)
            self.assertEqual(cfg['temperature_limit'], 70)
            self.assertEqual(cfg['power_limit'], 660)
            self.assertEqual(cfg['imbalance_percent'], 40)
            self.assertEqual(cfg['hard_shutdown_mask'], 63)
            self.assertEqual(cfg['soft_shutdown_mask'], 0)
            with self.assertRaises(ValueError):
                protocol.decode_configuration(payload[:-1])

    def test_pin_colors_use_reference_and_lower_device_setting(self):
        self.assertEqual(limits.pin_reference({'wire_current_limit': 10.5}), 9.2)
        self.assertEqual(limits.pin_reference({'wire_current_limit': 8}), 8)
        self.assertEqual(limits.pin_reference({'wire_current_limit': 0}), 9.2)
        self.assertEqual(limits.load_level(8.0, 9.2), 'normal')
        self.assertEqual(limits.load_level(8.3, 9.2), 'near')
        self.assertEqual(limits.load_level(9.2, 9.2), 'high')
        self.assertEqual(limits.load_level(10.5, 9.2), 'high')
        self.assertEqual(limits.load_level(None, 9.2), 'unknown')
        self.assertEqual(limits.load_level(10, 0), 'unknown')

    def test_configuration_response_is_fully_drained(self):
        payload = bytearray(96)
        payload[2] = 2
        payload[57] = 105
        class FakeSerial:
            def __init__(self):
                self.data = payload + b'next sample'
                self.commands = []
            def write(self, command):
                self.commands.append(command)
            def read(self, size):
                result, self.data = self.data[:size], self.data[size:]
                return result
        device = object.__new__(protocol.Device)
        device.serial = FakeSerial()
        self.assertEqual(device.configuration()['wire_current_limit'], 10.5)
        self.assertEqual(device.serial.data, b'next sample')
        self.assertEqual(device.serial.commands, [b'\x05'])

    def test_full_load_and_signed_temperature(self):
        sample = protocol.decode(packet())
        self.assertAlmostEqual(sample['power'], 599.976)
        self.assertAlmostEqual(sample['current'], 49.998)
        self.assertEqual(sample['temperatures'], [26.4, 31.0, None, -5.0])
        self.assertEqual(sample['voltage'], 12.0)
        self.assertEqual(sample['fan'], 42)
        self.assertEqual(sample['cable_watts'], 600)
        self.assertEqual(protocol.fault_names(sample['active_faults']), ['Current imbalance'])
        self.assertEqual(protocol.fault_names(sample['logged_faults']), ['Chip temperature', 'Unknown bits 0x8000'])

    def test_rejects_corrupt_samples(self):
        with self.assertRaises(ValueError):
            protocol.decode(packet()[:99])
        payload = packet()
        payload[94] = 4
        with self.assertRaises(ValueError):
            protocol.decode(payload)
        payload = packet()
        struct.pack_into('<I', payload, 16, 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            protocol.decode(payload)

    def test_mutating_commands_blocked(self):
        device = object.__new__(protocol.Device)
        for command in (6, 8, 9, 11, 12, 14, 0xF0, 0xF1, 0xF2):
            with self.assertRaises(ValueError):
                device.read_command(command, 1)

    def test_worker_closes_failed_connection_and_reconnects(self):
        loader = importlib.machinery.SourceFileLoader('monitor_for_test', 'monitor.pyw')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        monitor = importlib.util.module_from_spec(spec)
        loader.exec_module(monitor)
        stop = threading.Event()
        events = queue.Queue(maxsize=1)
        statuses = []
        devices = []

        class FakeDevice:
            port, firmware, uid = 'COM_TEST', 5, 'TEST'
            def __init__(self):
                self.closed = False
                self.ordinal = len(devices)
                devices.append(self)
            def sample(self):
                if self.ordinal == 0:
                    raise TimeoutError('USB unplugged')
                stop.set()
                return protocol.decode(packet())
            def configuration(self):
                return {'wire_current_limit': 10.5}
            def close(self):
                self.closed = True

        class ImmediateStop:
            def is_set(self):
                return stop.is_set()
            def wait(self, seconds):
                return stop.is_set()

        with patch.object(monitor, 'Device', FakeDevice), patch.object(monitor, 'publish_status', statuses.append), patch.object(monitor.logging, 'info'), patch.object(monitor.logging, 'warning'):
            monitor.worker(events, ImmediateStop())
        self.assertEqual([s['state'] for s in statuses], ['disconnected', 'live', 'stopped'])
        self.assertTrue(all(d.closed for d in devices))
        self.assertEqual(events.get_nowait()['state'], 'live')


if __name__ == '__main__':
    unittest.main()
