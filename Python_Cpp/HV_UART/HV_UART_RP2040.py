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

# Utility function to calculate 2-byte ASCII-encoded checksum
def calculate_checksum(command_bytes):
    checksum_total = sum(command_bytes) & 0xFF  # Only lower byte is used
    checksum_hex = f"{checksum_total:02X}"      # Format as two-character hex
    checksum_high = ord(checksum_hex[0])        # High nibble ASCII
    checksum_low = ord(checksum_hex[1])         # Low nibble ASCII
    return bytearray([checksum_high, checksum_low])

# Function to send a command with checksum and receive response
def send_command(command):
    command_bytes = bytearray([0x02]) + command + bytearray([0x03])
    checksum = calculate_checksum(command_bytes)
    command_bytes += checksum + bytearray([0x0D])

    print("Sending command:", command_bytes)
    uart.write(command_bytes)
    time.sleep(0.1)

    time.sleep(1)
    response = uart.read()
    print("Received response:", response)
    return response

# Relay control functions
def relay_on():
    relay.on()
    print("Relay ON - HV output connected")

def relay_off():
    relay.off()
    print("Relay OFF - HV output disconnected")

# PSU control functions
def hv_on():
    relay_on()  # Ensure relay is ON before turning on HV
    response = send_command(HV_ON)
    if response and b'0002' not in response:
        print("High Voltage ON successful:", response)
    else:
        print("Failed to turn on High Voltage:", response)

def hv_off():
    response = send_command(HV_OFF)
    if response and b'0002' not in response:
        print("High Voltage OFF successful:", response)
    else:
        print("Failed to turn off High Voltage:", response)
    relay_off()  # Turn relay OFF after HV is off

def get_voltage():
    response = send_command(GET_VOLTAGE)
    if response and b'0002' not in response:
        voltage_hex = response[4:8].decode('ascii')
        voltage_val = int(voltage_hex, 16) * 1.812e-3
        print("Output Voltage:", voltage_val, "V")
        return voltage_val
    else:
        print("Failed to retrieve output voltage")
        return None

def get_current():
    response = send_command(GET_CURRENT)
    if response and b'0002' not in response:
        current_hex = response[4:8].decode('ascii')
        current_val = int(current_hex, 16) * 4.98e-3
        print("Output Current:", current_val, "mA")
        return current_val
    else:
        print("Failed to retrieve output current")
        return None

def get_status():
    response = send_command(GET_STATUS)
    if response and b'0002' not in response:
        print("PSU Status:", response)
        return response
    else:
        print("Failed to retrieve PSU status")
        return None

# Main control procedure for testing and operation
def main():
    print("Initializing PSU control sequence...")

    # Initial delay for PSU to power up and stabilize
    time.sleep(5)

    # Turn on HV and read values
    hv_on()
    time.sleep(2)  # Allow time for stabilization
    voltage = get_voltage()
    current = get_current()

    # Perform status check
    status = get_status()
    
    # Turn off HV after operation
    hv_off()

    print("Control sequence completed.")

# Run the main procedure
main()
