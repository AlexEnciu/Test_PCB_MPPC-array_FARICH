from machine import Pin, PWM
import utime
import sys

# Pin Setup
pwm = PWM(Pin(7))  # PWM on GP7
pwm.freq(200000)   # Default PWM frequency = 200kHz

digital_control = Pin(6, Pin.OUT)  # Digital ON/OFF control

# Function to set PWM duty cycle
def set_pwm_duty(duty_cycle):
    """Set PWM duty cycle (0-100%)."""
    if 0 <= duty_cycle <= 100:
        pwm.duty_u16(int(duty_cycle * 65535 / 100))
        print(f"PWM set to {duty_cycle}%")
    else:
        print("Error: Duty cycle must be between 0 and 100.")

# Function to control LED ON/OFF
def set_led_state(state):
    """Turn LED ON or OFF."""
    if state == "ON":
        digital_control.value(1)
        print("LED turned ON")
    elif state == "OFF":
        digital_control.value(0)
        print("LED turned OFF")
    else:
        print("Invalid command. Use ON or OFF.")

# Main loop to read serial commands
print("RP2040 Serial Control Ready. Use commands: ON, OFF, PWM <0-100>")

try:
    while True:
        # Check if there is serial input available
        if sys.stdin.readable():
            command = sys.stdin.readline().strip().upper()  # Read input
            if command.startswith("PWM"):
                try:
                    duty = int(command.split()[1])  # Extract duty cycle
                    set_pwm_duty(duty)
                except (IndexError, ValueError):
                    print("Invalid PWM command. Use: PWM <0-100>")
            elif command == "ON":
                set_led_state("ON")
            elif command == "OFF":
                set_led_state("OFF")
            elif command == "EXIT":
                print("Exiting...")
                break
            else:
                print("Unknown command. Use ON, OFF, PWM <0-100>, EXIT")

        utime.sleep(0.1)  # Small delay to avoid CPU overload

except KeyboardInterrupt:
    print("\nProgram stopped.")

finally:
    pwm.deinit()  # Stop PWM safely
    digital_control.value(0)
    print("PWM and LED control deinitialized.")
