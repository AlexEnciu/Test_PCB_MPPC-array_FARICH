from machine import Pin, I2C
from time import sleep_ms

# ============================================================
# Hardware configuration
# ============================================================

I2C_BUS = 0
SDA_PIN = 0
SCL_PIN = 1
I2C_FREQUENCY = 100_000

DAC_ADDRESS = 0x60

# Enter the voltage measured directly between DAC VDD and GND.
DAC_VDD_VOLTS = 3.300
TARGET_OUT6_VOLTS = 1.000

# 12-bit DAC
DAC_STEPS = 4096
DAC_MAX_CODE = 0x0FFF

# Volatile registers
REG_VREF = 0x08
REG_POWER_DOWN = 0x09
REG_GAIN_STATUS = 0x0A
REG_WIPERLOCK = 0x0B

# MCP47FEB28 command fields
WRITE_COMMAND = 0x00       # C1:C0 = 00
READ_COMMAND = 0x06        # C1:C0 = 11

# Mapping from DAC channel to your schematic net
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

# ============================================================
# Initialize I2C
# ============================================================

i2c = I2C(
    I2C_BUS,
    sda=Pin(SDA_PIN),
    scl=Pin(SCL_PIN),
    freq=I2C_FREQUENCY
)


# ============================================================
# Low-level DAC functions
# ============================================================

def make_command(register, command):
    if register < 0 or register > 0x1F:
        raise ValueError("Invalid register address")

    return ((register & 0x1F) << 3) | command


def write_register(register, value):
    if value < 0 or value > 0xFFFF:
        raise ValueError("Invalid register value")

    command = make_command(register, WRITE_COMMAND)

    packet = bytes((
        command,
        (value >> 8) & 0xFF,
        value & 0xFF
    ))

    acknowledged = i2c.writeto(DAC_ADDRESS, packet)

    if acknowledged is not None and acknowledged != len(packet):
        raise OSError("Incomplete I2C write")


def read_register(register):
    command = make_command(register, READ_COMMAND)

    # False is positional, not stop=False.
    # This generates the required repeated START.
    acknowledged = i2c.writeto(
        DAC_ADDRESS,
        bytes((command,)),
        False
    )

    if acknowledged is not None and acknowledged != 1:
        raise OSError("DAC did not acknowledge read command")

    data = i2c.readfrom(DAC_ADDRESS, 2)

    if len(data) != 2:
        raise OSError("DAC did not return two bytes")

    return (data[0] << 8) | data[1]


# ============================================================
# Voltage conversion
# ============================================================

def voltage_to_code(voltage):
    if voltage <= 0:
        return 0

    if voltage >= DAC_VDD_VOLTS:
        return DAC_MAX_CODE

    code = int(
        voltage * DAC_STEPS / DAC_VDD_VOLTS + 0.5
    )

    return min(max(code, 0), DAC_MAX_CODE)


def code_to_voltage(code):
    return DAC_VDD_VOLTS * code / DAC_STEPS


# ============================================================
# Readback checking
# ============================================================

def check_register(label, actual, expected, mask=0xFFFF):
    passed = (actual & mask) == (expected & mask)

    if passed:
        print(
            "READ PASS: {} = 0x{:04X}".format(
                label, actual
            )
        )
    else:
        print(
            "READ FAIL: {} = 0x{:04X}, expected 0x{:04X}".format(
                label, actual, expected
            )
        )

    return passed


# ============================================================
# Main test
# ============================================================

def main():
    print()
    print("MCP47FEB28 12-bit DAC test")
    print(
        "SDA=GP{}, SCL=GP{}, frequency={} Hz".format(
            SDA_PIN,
            SCL_PIN,
            I2C_FREQUENCY
        )
    )

    devices = i2c.scan()

    print(
        "I2C devices:",
        ["0x{:02X}".format(address) for address in devices]
    )

    if DAC_ADDRESS not in devices:
        print("TEST STOPPED: DAC 0x60 not found")
        print("Check power, common ground, SDA, SCL and pull-ups")
        return

    print("Address 0x60: ACK")

    # --------------------------------------------------------
    # Configure volatile registers
    # --------------------------------------------------------

    # Every channel uses VDD as its reference.
    write_register(REG_VREF, 0x0000)
    print("WRITE PASS: VREF = VDD")

    # Every channel enabled and operating normally.
    write_register(REG_POWER_DOWN, 0x0000)
    print("WRITE PASS: all channels enabled")

    # Gain bits G7:G0 = 0, therefore gain = 1x.
    write_register(REG_GAIN_STATUS, 0x0000)
    print("WRITE PASS: all channel gains = 1x")

    # --------------------------------------------------------
    # Clear all eight outputs
    # --------------------------------------------------------

    for channel in range(8):
        write_register(channel, 0x0000)

    # --------------------------------------------------------
    # Set out_6 / DAC0 to 1 V
    # --------------------------------------------------------

    target_code = voltage_to_code(TARGET_OUT6_VOLTS)

    write_register(0x00, target_code)

    sleep_ms(5)

    print()
    print("DAC values written:")

    for channel in range(8):
        expected = target_code if channel == 0 else 0

        print(
            "  {} / DAC{} = 0x{:04X}".format(
                CHANNEL_NAMES[channel],
                channel,
                expected
            )
        )

    # --------------------------------------------------------
    # Configuration readback
    # --------------------------------------------------------

    print()
    print("Configuration register readback:")

    test_passed = True

    vref = read_register(REG_VREF)

    test_passed &= check_register(
        "VREF",
        vref,
        0x0000
    )

    power_down = read_register(REG_POWER_DOWN)

    test_passed &= check_register(
        "POWER_DOWN",
        power_down,
        0x0000
    )

    gain_status = read_register(REG_GAIN_STATUS)

    # Only bits 15:8 contain the eight channel gain bits.
    test_passed &= check_register(
        "GAIN bits",
        gain_status,
        0x0000,
        mask=0xFF00
    )

    wiperlock = read_register(REG_WIPERLOCK)

    test_passed &= check_register(
        "WIPERLOCK",
        wiperlock,
        0x0000
    )

    # --------------------------------------------------------
    # DAC channel readback
    # --------------------------------------------------------

    print()
    print("DAC register readback:")

    for channel in range(8):
        expected = target_code if channel == 0 else 0

        actual = read_register(channel)
        actual &= DAC_MAX_CODE

        test_passed &= check_register(
            "{} / DAC{}".format(
                CHANNEL_NAMES[channel],
                channel
            ),
            actual,
            expected,
            mask=DAC_MAX_CODE
        )

    expected_voltage = code_to_voltage(target_code)

    print()
    print(
        "Requested out_6 voltage: {:.3f} V".format(
            TARGET_OUT6_VOLTS
        )
    )
    print(
        "DAC VDD/reference: {:.3f} V".format(
            DAC_VDD_VOLTS
        )
    )
    print(
        "DAC0 code: {} / 0x{:04X}".format(
            target_code,
            target_code
        )
    )
    print(
        "Ideal out_6 voltage: {:.6f} V".format(
            expected_voltage
        )
    )
    print("Expected other outputs: approximately 0 V")

    print()

    if test_passed:
        print("TEST PASSED")
        print("Measure out_6 relative to the DAC circuit ground")
    else:
        print("TEST FAILED")
        print("Check LAT0 and LAT1 directly at the DAC pins")


# ============================================================
# Run
# ============================================================

try:
    main()

except OSError as error:
    print()
    print("I2C ERROR:", error)
    print("Check ground, pull-ups, wiring and bus contention")