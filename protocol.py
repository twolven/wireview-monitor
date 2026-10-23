"""Read-only WireView Pro II USB telemetry. No configuration or firmware writes."""
import struct
import time

import serial
from serial.tools import list_ports

FAULTS = ('Chip temperature', 'Sensor temperature', 'Total current',
          'Per-wire current', 'Total power', 'Current imbalance')


def decode_configuration(payload):
    if len(payload) < 4:
        raise ValueError('Short configuration header')
    version = payload[2]
    if version not in (0, 1, 2) or len(payload) != (72, 74, 96)[version]:
        raise ValueError('Unsupported or incomplete configuration')
    return dict(version=version, read_at=time.time(),
                display_mask=struct.unpack_from('<H', payload, 46)[0],
                buzzer_mask=struct.unpack_from('<H', payload, 48)[0],
                soft_shutdown_mask=struct.unpack_from('<H', payload, 50)[0],
                hard_shutdown_mask=struct.unpack_from('<H', payload, 52)[0],
                temperature_limit=struct.unpack_from('<h', payload, 54)[0]/10,
                current_limit=payload[56], wire_current_limit=payload[57]/10,
                power_limit=struct.unpack_from('<H', payload, 58)[0],
                imbalance_percent=payload[60], imbalance_min_current=payload[61],
                shutdown_wait=payload[62])


def decode(payload):
    if len(payload) != 100:
        raise ValueError(f'Expected 100 sensor bytes, received {len(payload)}')
    temps = [v / 10 for v in struct.unpack_from('<4h', payload)]
    pins = []
    for n in range(6):
        offset = 12 + n * 12
        voltage = struct.unpack_from('<h', payload, offset)[0] / 1000
        current = struct.unpack_from('<I', payload, offset + 4)[0] / 1000
        if not 0 <= voltage <= 20 or not 0 <= current <= 100:
            raise ValueError('Invalid pin sample; serial stream may be out of sync')
        pins.append(dict(voltage=voltage, current=current, power=voltage * current))
    capability = payload[94]
    if capability > 3:
        raise ValueError('Unknown cable capability')
    active, logged = struct.unpack_from('<HH', payload, 96)
    return dict(timestamp=time.time(), temperatures=[None if v == -100 else v for v in temps],
                pins=pins, power=sum(p['power'] for p in pins),
                current=sum(p['current'] for p in pins),
                voltage=struct.unpack_from('<H', payload, 92)[0] / 1000,
                fan=payload[10], cable_watts=(600, 450, 300, 150)[capability],
                active_faults=active, logged_faults=logged)


def fault_names(mask):
    names = [name for n, name in enumerate(FAULTS) if mask & (1 << n)]
    if mask & ~0x3f:
        names.append(f'Unknown bits 0x{mask & ~0x3f:04X}')
    return names


class Device:
    def __init__(self):
        ports = [p for p in list_ports.comports() if p.vid == 0x0483 and p.pid == 0x5740]
        if len(ports) != 1:
            raise OSError(f'Expected one WireView USB device; found {len(ports)}')
        self.port = ports[0].device
        self.serial = serial.Serial()
        self.serial.port = self.port
        self.serial.baudrate = 115200
        self.serial.timeout = 1
        self.serial.write_timeout = 1
        self.serial.rts = False
        self.serial.dtr = False
        try:
            self.serial.open()
            self.serial.reset_input_buffer()
            self.serial.rts = True
            greeting = self.serial.read(32)
            self.serial.rts = False
            if greeting and greeting != b'Thermal Grizzly WireView Pro II\0\0':
                # The product greeting is 30 text bytes padded to 32 bytes.
                if greeting.rstrip(b'\0') != b'Thermal Grizzly WireView Pro II':
                    raise ValueError(f'Unexpected USB greeting: {greeting!r}')
            self.serial.reset_input_buffer()
            vendor = self.read_command(1, 3)
            if vendor[:2] not in (b'\xef\x05', b'\xef\x06'):
                raise ValueError(f'Unexpected WireView identity: {vendor.hex()}')
            self.firmware = vendor[2]
            self.uid = self.read_command(2, 12).hex()
        except Exception:
            self.close()
            raise

    def read_command(self, command, length):
        # Only these telemetry/identity commands are allowed by this client.
        if command not in (1, 2, 4, 5):
            raise ValueError('Command is not read-only telemetry')
        self.serial.write(bytes([command]))
        data = self.serial.read(length)
        if len(data) != length:
            raise TimeoutError(f'Short USB response: {len(data)}/{length} bytes')
        return data

    def sample(self):
        return decode(self.read_command(4, 100))

    def configuration(self):
        header = self.read_command(5, 4)
        version = header[2]
        if version not in (0, 1, 2):
            raise ValueError(f'Unsupported configuration version {version}')
        # Consume the whole response before sending any further command.
        tail = self.serial.read((72, 74, 96)[version]-4)
        return decode_configuration(header+tail)

    def close(self):
        if self.serial.is_open:
            self.serial.close()


if __name__ == '__main__':
    import json
    device = Device()
    try:
        print(json.dumps(dict(port=device.port, firmware=device.firmware, uid=device.uid)))
        for _ in range(3):
            print(json.dumps(device.sample()))
            time.sleep(0.5)
    finally:
        device.close()
