"""Hardware-free tests for the MicroPython firmware driver and commands."""

import importlib.util
from pathlib import Path
import sys
import time
import types
import unittest


FIRMWARE_PATH = Path(__file__).resolve().parents[1] / "firmware" / "main.py"


class FakePin:
    def __init__(self, number):
        self.number = number


class FakeI2C:
    def __init__(self, *args, **kwargs):
        addresses = list(range(0x00, 0x0C)) + list(range(0x10, 0x1B))
        self.memory = {address: 0 for address in addresses}
        self.memory[0x1A] = 0x0060
        self.pointer = 0

    def scan(self):
        return [0x60]

    def writeto(self, address, data, stop=True):
        if address != 0x60:
            raise OSError("unexpected address")
        command = data[0]
        register = command >> 3
        operation = (command >> 1) & 0x03
        if len(data) == 1:
            if operation != 3:
                raise OSError("expected read command")
            self.pointer = register
        else:
            if operation != 0:
                raise OSError("expected write command")
            value = (data[1] << 8) | data[2]
            self.memory[register] = value & 0xFF00 if register == 0x0A else value
        return len(data)

    def readfrom(self, address, count):
        if address != 0x60 or count != 2:
            raise OSError("unexpected read")
        value = self.memory[self.pointer]
        return bytes((value >> 8, value & 0xFF))


class FirmwareProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_machine = types.ModuleType("machine")
        fake_machine.Pin = FakePin
        fake_machine.I2C = FakeI2C
        fake_machine.unique_id = lambda: b"12345678"
        sys.modules["machine"] = fake_machine

        spec = importlib.util.spec_from_file_location("dac_firmware_test", FIRMWARE_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

        cls.module.time.ticks_ms = lambda: int(time.monotonic() * 1000)
        cls.module.time.ticks_diff = lambda current, previous: current - previous
        cls.module.time.sleep_ms = lambda milliseconds: time.sleep(milliseconds / 1000)

    def setUp(self):
        self.module.i2c.memory.update({address: 0 for address in self.module.i2c.memory})
        self.module.i2c.memory[0x1A] = 0x0060
        self.module.dac.por_seen = False

    def test_hello(self):
        result = self.module.handle_request({"cmd": "hello"})
        self.assertTrue(result["dac_present"])
        self.assertEqual(result["device"], "MCP47FEB28")
        self.assertEqual(result["protocol_version"], 1)

    def test_set_and_read_channel(self):
        self.module.handle_request(
            {
                "cmd": "set_channel",
                "channel": 0,
                "code": 0x04D9,
                "vref": 0,
                "gain": 0,
                "power_down": 0,
            }
        )
        state = self.module.handle_request({"cmd": "get_state"})
        self.assertEqual(state["channels"][0]["code"], 0x04D9)

    def test_eeprom_save_and_load(self):
        codes = list(range(8))
        self.module.handle_request(
            {
                "cmd": "apply_state",
                "codes": codes,
                "vrefs": [0] * 8,
                "gains": [0] * 8,
                "power_downs": [0] * 8,
            }
        )
        saved = self.module.handle_request(
            {"cmd": "save_eeprom", "confirm": "SAVE"}
        )
        self.assertEqual(saved["write_count"], 7)

        self.module.handle_request({"cmd": "zero_all"})
        self.module.handle_request({"cmd": "load_eeprom"})
        state = self.module.handle_request({"cmd": "get_state"})
        self.assertEqual([item["code"] for item in state["channels"]], codes)

    def test_eeprom_requires_confirmation(self):
        with self.assertRaises(ValueError):
            self.module.handle_request({"cmd": "save_eeprom"})


if __name__ == "__main__":
    unittest.main()
