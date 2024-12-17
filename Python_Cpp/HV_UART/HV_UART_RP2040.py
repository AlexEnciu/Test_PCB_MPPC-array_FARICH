from machine import UART, Pin
import time

# Configure UART1 on GPIO 8 (TX) and GPIO 9 (RX) with even parity
uart = UART(1, baudrate=38400, bits=8, parity=0, stop=1, tx=Pin(8), rx=Pin(9))

# Configure Relay on GPIO 5 for controlling high voltage connection
relay = Pin(5, Pin.OUT)
relay.off()  # Ensure relay is off initially (HV output disconnected)

# Command Constants
HV_ON = b'HON'          # High Voltage ON
HV_OFF = b'HOF'         # High Voltage OFF
GET_VOLTAGE = b'HGV'    # Get Voltage
GET_CURRENT = b'HGC'    # Get Current
GET_STATUS = b'HGS'     # Get Status
SET_VOLTAGE_PREFIX = b'HBV'  # Prefix for setting voltage

# ASCII characters for CLI design
LINE = "─" * 40
SECTION_DIVIDER = f"\n{LINE}\n"

# Utility function to calculate 2-byte ASCII-encoded checksum
def calculate_checksum(command_bytes):
    checksum_total = sum(command_bytes) & 0xFF  # Only lower byte is used
    checksum_hex = f"{checksum_total:02X}"      # Format as two-character hex
    checksum_high = ord(checksum_hex[0])        # High nibble ASCII
    checksum_low = ord(checksum_hex[1])         # Low nibble ASCII
    return bytearray([checksum_high, checksum_low])

# Function to interpret error codes
def interpret_error(response):
    error_code = response[4:8].decode('ascii')
    error_messages = {
        "0001": "UART communication error: Parity, overrun, or framing error.",
        "0002": "Timeout error: CR not received within 1000 ms of STX.",
        "0003": "Syntax error: Command does not start with STX or exceeds length.",
        "0004": "Checksum error: Checksum does not match.",
        "0005": "Command error: Undefined command.",
        "0006": "Parameter error: Non-hexadecimal character in parameter.",
        "0007": "Parameter size error: Parameter length is incorrect."
    }
    return error_messages.get(error_code, f"Unknown error code: {error_code}")

# Function to interpret status bits
def interpret_status(status_bits):
    descriptions = [
        ("High Voltage Output", "ON" if status_bits & (1 << 0) else "OFF"),
        ("Overcurrent Protection", "Active" if status_bits & (1 << 1) else "Inactive"),
        ("Output Current", "Out of spec" if status_bits & (1 << 2) else "Within spec"),
        ("Temperature Sensor", "Connected" if status_bits & (1 << 3) else "Disconnected"),
        ("Operating Temperature", "Out of spec" if status_bits & (1 << 4) else "Within spec"),
        ("Temperature Correction", "Active" if status_bits & (1 << 6) else "Inactive"),
        ("Automatic Restoration", "Active" if status_bits & (1 << 10) else "Inactive"),
        ("Voltage Suppression", "Active" if status_bits & (1 << 11) else "Inactive"),
        ("Output Voltage Control", "Active" if status_bits & (1 << 12) else "Inactive"),
        ("Voltage Stability", "Stable" if status_bits & (1 << 14) else "Unstable")
    ]
    return descriptions

# Function to send a command with checksum and receive response
def send_command(command):
    command_bytes = bytearray([0x02]) + command + bytearray([0x03])
    checksum = calculate_checksum(command_bytes)
    command_bytes += checksum + bytearray([0x0D])

    print(f"Sending: {command_bytes}")
    uart.write(command_bytes)
    time.sleep(0.1)

    # Wait for response and read all available bytes
    time.sleep(1)
    response = uart.read()
    if response:
        print(f"Received: {response}")
        # Check if the response indicates an error
        if response[2:5] == b'hxx' and response[5:8].isdigit():
            error_message = interpret_error(response)
            print(f"[ERROR] {error_message}")
            return None
    else:
        print("[WARNING] No response received.")
    return response

# Relay control functions
def relay_on():
    relay.on()
    print("[INFO] Relay ON - HV output connected")

def relay_off():
    relay.off()
    print("[INFO] Relay OFF - HV output disconnected")

# PSU control functions
def hv_on():
    """Turn on the high voltage output, check status to confirm."""
    relay_on()  # Ensure relay is ON before turning on HV
    send_command(HV_ON)  # Send command to turn on high voltage
    time.sleep(1)  # Wait briefly before checking the status

    status = get_status()  # Get the current status

    # Check if high voltage is actually ON based on status output
    if status:
        for desc, state in status:
            if desc == "High Voltage Output":
                if state == "ON":
                    print("[SUCCESS] High Voltage is ON (confirmed by status check)")
                else:
                    print("[FAILURE] High Voltage ON failed (confirmed by status check)")
                break
    else:
        print("[FAILURE] Unable to retrieve PSU status for verification")


def hv_off():
    """Turn off the high voltage output, check status to confirm."""
    send_command(HV_OFF)  # Send command to turn off high voltage
    time.sleep(1)
    status = get_status()  # Get the current status

    # Check if high voltage is actually OFF based on status output
    if status:
        for desc, state in status:
            if desc == "High Voltage Output":
                if state == "OFF":
                    print("[SUCCESS] High Voltage is OFF (confirmed by status check)")
                else:
                    print("[FAILURE] High Voltage OFF failed (confirmed by status check)")
                break
    else:
        print("[FAILURE] Unable to retrieve PSU status for verification")

    relay_off()  # Turn relay OFF after HV is off

def get_voltage():
    response = send_command(GET_VOLTAGE)
    if response:
        voltage_hex = response[4:8].decode('ascii')
        voltage_val = int(voltage_hex, 16) * 1.812e-3
        print(f"[VOLTAGE] Output Voltage: {voltage_val:.3f} V")
        return voltage_val
    else:
        print("[FAILURE] Voltage retrieval failed")
        return None

def get_current():
    response = send_command(GET_CURRENT)
    if response:
        current_hex = response[4:8].decode('ascii')
        current_val = int(current_hex, 16) * 4.98e-3
        print(f"[CURRENT] Output Current: {current_val:.3f} mA")
        return current_val
    else:
        print("[FAILURE] Current retrieval failed")
        return None

# Enhanced function to get and interpret PSU status
def get_status():
    response = send_command(GET_STATUS)
    if response:
        status_hex = response[4:8].decode('ascii')
        status_bits = int(status_hex, 16)  # Convert hex to integer
        print(SECTION_DIVIDER + "PSU STATUS" + SECTION_DIVIDER)
        
        # Interpret each status bit
        status_descriptions = interpret_status(status_bits)
        for desc, state in status_descriptions:
            print(f"{desc:<25} : {state}")
        print(LINE)
        return status_descriptions
    else:
        print("[FAILURE] Status retrieval failed")
        return None

def set_voltage(target_voltage):
    """Set the high voltage to a specified target and confirm it is achieved."""
    # Check if target voltage is within the allowable range
    if not (20 <= target_voltage <= 90):
        print(f"[ERROR] Target voltage {target_voltage} V is out of range (20-90 V).")
        return

    # Ensure HV is ON before attempting to set voltage
    status = get_status()
    hv_on_state = any(desc == "High Voltage Output" and state == "ON" for desc, state in status)

    if not hv_on_state:
        print("[WARNING] High Voltage is OFF. Turning ON before setting voltage.")
        hv_on()

    # Convert voltage to hexadecimal representation
    target_voltage_hex = f"{int(target_voltage / 1.812e-3):04X}"
    command = SET_VOLTAGE_PREFIX + target_voltage_hex.encode('ascii')

    send_command(command)  # Send command to set voltage

    # Confirm the set voltage by reading back the actual output voltage
    time.sleep(1)  # Wait for voltage to stabilize
    actual_voltage = get_voltage()
    
    # Check if actual voltage is within a small margin (e.g., ±1%)
    if actual_voltage and abs(actual_voltage - target_voltage) / target_voltage <= 0.01:
        print(f"[SUCCESS] Voltage set to {target_voltage:.3f} V (confirmed)")
    else:
        print(f"[FAILURE] Voltage setting failed. Target: {target_voltage:.3f} V, Actual: {actual_voltage:.3f} V")

# Main control procedure for testing and operation
def main():
    print(SECTION_DIVIDER + "PSU CONTROL SEQUENCE" + SECTION_DIVIDER)

    # Initial delay for PSU to power up and stabilize
    print("[INFO] Initializing...")
    time.sleep(5)

    # Turn on HV and set voltage
    hv_on()
    set_voltage(75.0)  # Example voltage setting

    # Monitor and display readings
    time.sleep(2)  # Allow time for stabilization
    print(SECTION_DIVIDER + "REAL-TIME VOLTAGE AND CURRENT MONITORING" + SECTION_DIVIDER)

    try:
        while True:
            # Get and display voltage and current
            voltage = get_voltage()
            current = get_current()
            time.sleep(1)  # Update every second

            # In-place update to avoid scrolling
            print("\033[2J\033[H", end="")  # Clear screen and reset cursor
            print(SECTION_DIVIDER + "REAL-TIME VOLTAGE AND CURRENT MONITORING" + SECTION_DIVIDER)
            print(f"[VOLTAGE] Output Voltage: {voltage:.3f} V")
            print(f"[CURRENT] Output Current: {current:.3f} mA")
            print(LINE)

    except KeyboardInterrupt:
        # When interrupted, turn off HV and exit safely
        hv_off()
        print("\n" + SECTION_DIVIDER + "CONTROL SEQUENCE COMPLETED" + SECTION_DIVIDER)

# Run the main procedure
main()
