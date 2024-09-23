import serial

# Open the serial port (adjust the COM port as needed)
ser = serial.Serial('COM7', 38400, parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE, timeout=1)

# Construct the HOF command with correct checksum
hof_command = bytearray([0x02, 0x48, 0x4F, 0x46, 0x03, 0x00, 0xE2, 0x0D])

# Send the command
ser.write(hof_command)

# Read the response from the power supply
response = ser.read(100)  # Adjust buffer size as needed
print("Response:", response)

# Close the serial port
ser.close()
