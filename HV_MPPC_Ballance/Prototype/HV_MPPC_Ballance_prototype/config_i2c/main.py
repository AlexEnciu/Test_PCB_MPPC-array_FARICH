"""
USB DAC controller firmware for:

    Pimoroni Tiny 2040 (RP2040)
    MCP47FEB28, 12-bit, 8-channel DAC

The firmware exposes a newline-delimited JSON protocol on the MicroPython
USB serial connection.  The desktop application in this project is the
intended client.

Connections:
    Tiny 2040 GP0 -> DAC SDA
    Tiny 2040 GP1 -> DAC SCL
    Tiny 2040 GND -> DAC GND

Required board states for the default configuration:
    A0 = GND, A1 = GND       (7-bit I2C address 0x60)
    LAT0 = GND, LAT1 = GND   (immediate output updates)

The firmware does not change the DAC at boot.  EEPROM is written only after
an explicit save_eeprom request containing the confirmation word "SAVE".

To stop the service and return to the MicroPython REPL, connect with Thonny
and press Stop (Ctrl+C).
"""

import gc
import sys
import time

try:
    import ujson as json
except ImportError:
    import json

try:
    import uselect as select
except ImportError:
    import select

import machine
from machine import I2C, Pin


# ---------------------------------------------------------------------------
# Firmware and hardware configuration
# ---------------------------------------------------------------------------

FIRMWARE_NAME = "Tiny2040 MCP47FEB28 USB Controller"
FIRMWARE_VERSION = "1.0.0"
PROTOCOL_VERSION = 1

I2C_BUS = 0
SDA_PIN = 0
SCL_PIN = 1
I2C_FREQUENCY = 100_000
DAC_ADDRESS = 0x60

DAC_BITS = 12
DAC_STEPS = 1 << DAC_BITS
DAC_MAX_CODE = DAC_STEPS - 1
CHANNEL_COUNT = 8

CHANNEL_NAMES = (
    "out_6",  # DAC0 / VOUT0
    "out_7",  # DAC1 / VOUT1
    "out_4",  # DAC2 / VOUT2
    "out_5",  # DAC3 / VOUT3
    "out_2",  # DAC4 / VOUT4
    "out_3",  # DAC5 / VOUT5
    "out_0",  # DAC6 / VOUT6
    "out_1",  # DAC7 / VOUT7
)


# ---------------------------------------------------------------------------
# MCP47FEB28 driver
# ---------------------------------------------------------------------------


class MCP47FEB28:
    REG_DAC0 = 0x00
    REG_VREF = 0x08
    REG_POWER_DOWN = 0x09
    REG_GAIN_STATUS = 0x0A
    REG_WIPERLOCK_STATUS = 0x0B

    REG_NV_DAC0 = 0x10
    REG_NV_VREF = 0x18
    REG_NV_POWER_DOWN = 0x19
    REG_NV_GAIN_ADDRESS = 0x1A

    WRITE_COMMAND = 0x00
    READ_COMMAND = 0x06

    VALID_READ_REGISTERS = tuple(range(0x00, 0x0C)) + tuple(range(0x10, 0x1B))
    VALID_VOLATILE_WRITE_REGISTERS = tuple(range(0x00, 0x0B))

    def __init__(self, i2c, address):
        self.i2c = i2c
        self.address = address
        self.por_seen = False

    @staticmethod
    def _check_channel(channel):
        if not isinstance(channel, int) or channel < 0 or channel >= CHANNEL_COUNT:
            raise ValueError("channel must be an integer from 0 to 7")

    @staticmethod
    def _check_u16(value):
        if not isinstance(value, int) or value < 0 or value > 0xFFFF:
            raise ValueError("register value must be an integer from 0 to 65535")

    @classmethod
    def _make_command(cls, register, command):
        if register < 0 or register > 0x1F:
            raise ValueError("register must be between 0x00 and 0x1F")
        return ((register & 0x1F) << 3) | command

    def is_present(self):
        return self.address in self.i2c.scan()

    def write_register(self, register, value):
        self._check_u16(value)
        command = self._make_command(register, self.WRITE_COMMAND)
        packet = bytes((command, (value >> 8) & 0xFF, value & 0xFF))
        acknowledged = self.i2c.writeto(self.address, packet)
        if acknowledged is not None and acknowledged != len(packet):
            raise OSError("incomplete I2C write to register 0x{:02X}".format(register))

    def read_register(self, register):
        if register not in self.VALID_READ_REGISTERS:
            raise ValueError("register 0x{:02X} is reserved".format(register))

        command = self._make_command(register, self.READ_COMMAND)

        # The stop argument is positional on RP2040 MicroPython.
        acknowledged = self.i2c.writeto(self.address, bytes((command,)), False)
        if acknowledged is not None and acknowledged != 1:
            raise OSError("DAC did not acknowledge register 0x{:02X}".format(register))

        data = self.i2c.readfrom(self.address, 2)
        if len(data) != 2:
            raise OSError("DAC returned an incomplete register value")
        return (data[0] << 8) | data[1]

    @staticmethod
    def _replace_2bit_field(word, channel, value):
        if not isinstance(value, int) or value < 0 or value > 3:
            raise ValueError("two-bit field value must be from 0 to 3")
        shift = channel * 2
        return (word & ~(0x03 << shift)) | ((value & 0x03) << shift)

    @staticmethod
    def _replace_gain_bit(word, channel, value):
        if value not in (0, 1):
            raise ValueError("gain must be 0 (1x) or 1 (2x)")
        mask = 1 << (channel + 8)
        if value:
            return word | mask
        return word & ~mask

    def set_channel(self, channel, code, vref=None, gain=None, power_down=None):
        self._check_channel(channel)
        if not isinstance(code, int) or code < 0 or code > DAC_MAX_CODE:
            raise ValueError("code must be an integer from 0 to 4095")

        if vref is not None or gain is not None or power_down is not None:
            self.set_channel_config(channel, vref, gain, power_down)

        self.write_register(channel, code)

    def set_channel_config(self, channel, vref=None, gain=None, power_down=None):
        self._check_channel(channel)

        if vref is not None:
            if vref not in (0, 1, 2, 3):
                raise ValueError("vref must be 0, 1, 2, or 3")
            value = self.read_register(self.REG_VREF)
            value = self._replace_2bit_field(value, channel, vref)
            self.write_register(self.REG_VREF, value)

        if power_down is not None:
            if power_down not in (0, 1, 2, 3):
                raise ValueError("power_down must be 0, 1, 2, or 3")
            value = self.read_register(self.REG_POWER_DOWN)
            value = self._replace_2bit_field(value, channel, power_down)
            self.write_register(self.REG_POWER_DOWN, value)

        if gain is not None:
            if gain not in (0, 1):
                raise ValueError("gain must be 0 or 1")
            value = self.read_register(self.REG_GAIN_STATUS)
            value = self._replace_gain_bit(value, channel, gain)
            self.write_register(self.REG_GAIN_STATUS, value & 0xFF00)

    def apply_state(self, codes, vrefs, gains, power_downs):
        if not isinstance(codes, list) or len(codes) != CHANNEL_COUNT:
            raise ValueError("codes must contain exactly eight values")
        if not isinstance(vrefs, list) or len(vrefs) != CHANNEL_COUNT:
            raise ValueError("vrefs must contain exactly eight values")
        if not isinstance(gains, list) or len(gains) != CHANNEL_COUNT:
            raise ValueError("gains must contain exactly eight values")
        if not isinstance(power_downs, list) or len(power_downs) != CHANNEL_COUNT:
            raise ValueError("power_downs must contain exactly eight values")

        vref_word = 0
        power_word = 0
        gain_word = 0

        for channel in range(CHANNEL_COUNT):
            code = codes[channel]
            vref = vrefs[channel]
            gain = gains[channel]
            power_down = power_downs[channel]

            if not isinstance(code, int) or code < 0 or code > DAC_MAX_CODE:
                raise ValueError("channel {} code is outside 0..4095".format(channel))
            if vref not in (0, 1, 2, 3):
                raise ValueError("channel {} VREF mode is invalid".format(channel))
            if gain not in (0, 1):
                raise ValueError("channel {} gain is invalid".format(channel))
            if power_down not in (0, 1, 2, 3):
                raise ValueError("channel {} power-down mode is invalid".format(channel))

            vref_word |= (vref & 0x03) << (channel * 2)
            power_word |= (power_down & 0x03) << (channel * 2)
            gain_word |= (gain & 0x01) << (channel + 8)

        # Configuration first, output codes second. With LAT pins tied low,
        # every write affects its output immediately.
        self.write_register(self.REG_VREF, vref_word)
        self.write_register(self.REG_POWER_DOWN, power_word)
        self.write_register(self.REG_GAIN_STATUS, gain_word)

        for channel in range(CHANNEL_COUNT):
            self.write_register(channel, codes[channel])

    def read_state(self):
        vref_word = self.read_register(self.REG_VREF)
        power_word = self.read_register(self.REG_POWER_DOWN)
        gain_status = self.read_register(self.REG_GAIN_STATUS)
        wiperlock_word = self.read_register(self.REG_WIPERLOCK_STATUS)

        if gain_status & 0x0080:
            self.por_seen = True

        channels = []
        for channel in range(CHANNEL_COUNT):
            code = self.read_register(channel) & DAC_MAX_CODE
            channels.append({
                "channel": channel,
                "name": CHANNEL_NAMES[channel],
                "code": code,
                "vref": (vref_word >> (channel * 2)) & 0x03,
                "gain": (gain_status >> (channel + 8)) & 0x01,
                "power_down": (power_word >> (channel * 2)) & 0x03,
                "wiperlock": (wiperlock_word >> (channel * 2)) & 0x03,
            })

        return {
            "channels": channels,
            "status": {
                "por_seen": self.por_seen,
                "eeprom_busy": bool(gain_status & 0x0040),
            },
            "raw": {
                "vref": vref_word,
                "power_down": power_word,
                "gain_status": gain_status,
                "wiperlock": wiperlock_word,
            },
        }

    def read_eeprom(self):
        codes = []
        for channel in range(CHANNEL_COUNT):
            codes.append(self.read_register(self.REG_NV_DAC0 + channel) & DAC_MAX_CODE)

        vref_word = self.read_register(self.REG_NV_VREF)
        power_word = self.read_register(self.REG_NV_POWER_DOWN)
        gain_address = self.read_register(self.REG_NV_GAIN_ADDRESS)

        channels = []
        for channel in range(CHANNEL_COUNT):
            channels.append({
                "channel": channel,
                "name": CHANNEL_NAMES[channel],
                "code": codes[channel],
                "vref": (vref_word >> (channel * 2)) & 0x03,
                "gain": (gain_address >> (channel + 8)) & 0x01,
                "power_down": (power_word >> (channel * 2)) & 0x03,
            })

        return {
            "channels": channels,
            "address": gain_address & 0x7F,
            "address_locked": bool(gain_address & 0x0080),
            "raw": {
                "vref": vref_word,
                "power_down": power_word,
                "gain_address": gain_address,
                "dac": codes,
            },
        }

    def _wait_eeprom(self, timeout_ms=150):
        started = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), started) < timeout_ms:
            status = self.read_register(self.REG_GAIN_STATUS)
            if not (status & 0x0040):
                return
            time.sleep_ms(2)
        raise OSError("EEPROM write did not finish within {} ms".format(timeout_ms))

    def _write_eeprom_if_changed(self, register, value):
        current = self.read_register(register)
        if current == value:
            return False
        self.write_register(register, value)
        self._wait_eeprom()
        readback = self.read_register(register)
        if readback != value:
            raise OSError(
                "EEPROM verify failed at 0x{:02X}: 0x{:04X} != 0x{:04X}".format(
                    register, readback, value
                )
            )
        return True

    def save_current_to_eeprom(self):
        state = self.read_state()
        nv_gain_address = self.read_register(self.REG_NV_GAIN_ADDRESS)
        changed = []

        for channel in range(CHANNEL_COUNT):
            register = self.REG_NV_DAC0 + channel
            value = state["channels"][channel]["code"]
            if self._write_eeprom_if_changed(register, value):
                changed.append(register)

        if self._write_eeprom_if_changed(self.REG_NV_VREF, state["raw"]["vref"]):
            changed.append(self.REG_NV_VREF)

        if self._write_eeprom_if_changed(
            self.REG_NV_POWER_DOWN, state["raw"]["power_down"]
        ):
            changed.append(self.REG_NV_POWER_DOWN)

        # Preserve the existing address and address-lock bits. Only copy the
        # eight volatile gain bits into the EEPROM register's high byte.
        gain_address = (state["raw"]["gain_status"] & 0xFF00) | (nv_gain_address & 0x00FF)
        if self._write_eeprom_if_changed(self.REG_NV_GAIN_ADDRESS, gain_address):
            changed.append(self.REG_NV_GAIN_ADDRESS)

        return changed

    def load_eeprom_to_volatile(self):
        eeprom = self.read_eeprom()
        channels = eeprom["channels"]
        codes = [entry["code"] for entry in channels]
        vrefs = [entry["vref"] for entry in channels]
        gains = [entry["gain"] for entry in channels]
        power_downs = [entry["power_down"] for entry in channels]
        self.apply_state(codes, vrefs, gains, power_downs)


# ---------------------------------------------------------------------------
# USB JSON service
# ---------------------------------------------------------------------------


i2c = I2C(
    I2C_BUS,
    sda=Pin(SDA_PIN),
    scl=Pin(SCL_PIN),
    freq=I2C_FREQUENCY,
)

dac = MCP47FEB28(i2c, DAC_ADDRESS)


def _unique_id():
    try:
        raw = machine.unique_id()
        return "".join("{:02X}".format(value) for value in raw)
    except Exception:
        return "unknown"


def _device_info():
    devices = i2c.scan()
    return {
        "firmware": FIRMWARE_NAME,
        "firmware_version": FIRMWARE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "device": "MCP47FEB28",
        "resolution_bits": DAC_BITS,
        "channels": CHANNEL_COUNT,
        "dac_address": DAC_ADDRESS,
        "dac_present": DAC_ADDRESS in devices,
        "i2c_devices": devices,
        "i2c_bus": I2C_BUS,
        "i2c_frequency": I2C_FREQUENCY,
        "sda_pin": SDA_PIN,
        "scl_pin": SCL_PIN,
        "rp2040_uid": _unique_id(),
        "lat_mode": "external; expected LOW for immediate update",
    }


def _require_dac():
    if not dac.is_present():
        raise OSError("MCP47FEB28 was not found at I2C address 0x60")


def _parse_int(value, field_name):
    if isinstance(value, bool):
        raise ValueError("{} must be an integer".format(field_name))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            pass
    raise ValueError("{} must be an integer".format(field_name))


def _state_with_uptime():
    state = dac.read_state()
    state["uptime_ms"] = time.ticks_ms()
    return state


def handle_request(request):
    command = request.get("cmd")
    if not isinstance(command, str):
        raise ValueError("request is missing a string cmd field")

    if command == "hello" or command == "get_info":
        return _device_info()

    if command == "scan":
        return {"addresses": i2c.scan()}

    _require_dac()

    if command == "get_state":
        return _state_with_uptime()

    if command == "get_eeprom":
        return dac.read_eeprom()

    if command == "set_channel":
        channel = _parse_int(request.get("channel"), "channel")
        code = _parse_int(request.get("code"), "code")
        vref = request.get("vref")
        gain = request.get("gain")
        power_down = request.get("power_down")

        if vref is not None:
            vref = _parse_int(vref, "vref")
        if gain is not None:
            gain = _parse_int(gain, "gain")
        if power_down is not None:
            power_down = _parse_int(power_down, "power_down")

        dac.set_channel(channel, code, vref, gain, power_down)
        return {"state": _state_with_uptime()}

    if command == "apply_state":
        codes = [_parse_int(value, "code") for value in request.get("codes", [])]
        vrefs = [_parse_int(value, "vref") for value in request.get("vrefs", [])]
        gains = [_parse_int(value, "gain") for value in request.get("gains", [])]
        power_downs = [
            _parse_int(value, "power_down")
            for value in request.get("power_downs", [])
        ]
        dac.apply_state(codes, vrefs, gains, power_downs)
        return {"state": _state_with_uptime()}

    if command == "zero_all":
        for channel in range(CHANNEL_COUNT):
            dac.write_register(channel, 0)
        return {"state": _state_with_uptime()}

    if command == "save_eeprom":
        if request.get("confirm") != "SAVE":
            raise ValueError("EEPROM save requires confirm='SAVE'")
        changed = dac.save_current_to_eeprom()
        return {
            "changed_registers": changed,
            "write_count": len(changed),
            "eeprom": dac.read_eeprom(),
            "state": _state_with_uptime(),
        }

    if command == "load_eeprom":
        dac.load_eeprom_to_volatile()
        return {
            "eeprom": dac.read_eeprom(),
            "state": _state_with_uptime(),
        }

    if command == "raw_read":
        register = _parse_int(request.get("register"), "register")
        return {
            "register": register,
            "value": dac.read_register(register),
        }

    if command == "raw_write":
        register = _parse_int(request.get("register"), "register")
        value = _parse_int(request.get("value"), "value")
        if register not in dac.VALID_VOLATILE_WRITE_REGISTERS:
            raise ValueError("raw writes are limited to volatile registers 0x00..0x0A")
        dac.write_register(register, value)
        return {
            "register": register,
            "value": dac.read_register(register),
            "state": _state_with_uptime(),
        }

    raise ValueError("unknown command: {}".format(command))


def send_json(message):
    # separators is not available in every MicroPython ujson build.
    text = json.dumps(message)
    sys.stdout.write(text)
    sys.stdout.write("\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def run_service():
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)

    send_json({
        "event": "ready",
        "firmware": FIRMWARE_NAME,
        "version": FIRMWARE_VERSION,
        "protocol": PROTOCOL_VERSION,
        "dac_present": DAC_ADDRESS in i2c.scan(),
    })

    request_counter = 0
    idle_counter = 0

    while True:
        events = poller.poll(100)
        if not events:
            idle_counter += 1
            if idle_counter >= 100:
                gc.collect()
                idle_counter = 0
            continue

        idle_counter = 0

        line = sys.stdin.readline()
        if not line:
            time.sleep_ms(10)
            continue

        line = line.strip()
        if not line:
            continue

        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            request_id = request.get("id")
            result = handle_request(request)
            send_json({"id": request_id, "ok": True, "result": result})
        except KeyboardInterrupt:
            raise
        except Exception as error:
            send_json({
                "id": request_id,
                "ok": False,
                "error": {
                    "type": error.__class__.__name__,
                    "message": str(error),
                },
            })

        request_counter += 1
        if request_counter % 20 == 0:
            gc.collect()


if __name__ == "__main__":
    try:
        run_service()
    except KeyboardInterrupt:
        print("\nDAC USB service stopped. MicroPython REPL is available.")
