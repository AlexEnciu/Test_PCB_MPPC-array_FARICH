// Pin Definitions
const uint8_t pwmPin = 7;   // PWM output for LED brightness
const uint8_t ctrlPin = 6;  // Digital pin for ON/OFF control

void setup() {
    Serial.begin(115200); // Start serial communication
    pinMode(ctrlPin, OUTPUT);
    pinMode(pwmPin, OUTPUT);
    
    analogWrite(pwmPin, 0);  // Default PWM off
    digitalWrite(ctrlPin, HIGH); // Default LED off

      // Display welcome message with new ASCII art in cyan
  Serial.print("\033[36m"); // Set text color to cyan
  Serial.println("==============================================================================");
  Serial.println("   /$$   /$$       /$$     /$$       /$$$$$$$        /$$$$$$$         /$$$$$$ ");
  Serial.println("  | $$  | $$      |  $$   /$$/      | $$__  $$      | $$__  $$       /$$__  $$");
  Serial.println("  | $$  | $$       \\  $$ /$$/       | $$  \\ $$      | $$  \\ $$      | $$  \\ $$");
  Serial.println("  | $$$$$$$$        \\  $$$$/        | $$  | $$      | $$$$$$$/      | $$$$$$$$");
  Serial.println("  | $$__  $$         \\  $$/         | $$  | $$      | $$__  $$      | $$__  $$");
  Serial.println("  | $$  | $$          | $$          | $$  | $$      | $$  \\ $$      | $$  | $$");
  Serial.println("  | $$  | $$          | $$          | $$$$$$$/      | $$  | $$      | $$  | $$");
  Serial.println("  |__/  |__/          |__/          |_______/       |__/  |__/      |__/  |__/");
  Serial.println("                                                                         ");
  Serial.println("                                                                         ");
  Serial.println("                                                                         ");
  Serial.println("==============================================================================");
  Serial.print("\033[35m"); // Set text color to magenta
  Serial.println("UV LED Controller");
  Serial.print("\033[36m"); // Set text color back to cyan
  Serial.println("==============================================================================");
    Serial.println("\nAvailable Commands:");
    Serial.println("-------------------------------------------------");
    Serial.println("ON                - Turn LED ON");
    Serial.println("OFF               - Turn LED OFF");
    Serial.println("PWM <0-100>       - Set LED brightness (0-100%)");
    Serial.println("PULSE <width_us> <rate_Hz> <count> - Generate pulses");
    Serial.println("   width_us       - Pulse width in microseconds");
    Serial.println("   rate_Hz        - Pulses per second (Hz)");
    Serial.println("   count          - Total number of pulses");
    Serial.println("HELP              - Show this help menu");
    Serial.println("EXIT              - Turn everything off and stop execution");
  Serial.println("==============================================================================");
  Serial.print("\033[0m"); // Reset text color to default

    Serial.println("Tiny 2040 Serial Control Ready. Type HELP for available commands.");
}

// Function to display help menu
void showHelp() {
    Serial.println("\nAvailable Commands:");
    Serial.println("-------------------------------------------------");
    Serial.println("ON                - Turn LED ON");
    Serial.println("OFF               - Turn LED OFF");
    Serial.println("PWM <0-100>       - Set LED brightness (0-100%)");
    Serial.println("PULSE <width_us> <rate_Hz> <count> - Generate pulses");
    Serial.println("   width_us       - Pulse width in microseconds");
    Serial.println("   rate_Hz        - Pulses per second (Hz)");
    Serial.println("   count          - Total number of pulses");
    Serial.println("HELP              - Show this help menu");
    Serial.println("EXIT              - Turn everything off and stop execution");
    Serial.println("-------------------------------------------------");
}

// Function to create LED pulses with status updates
void pulseLED(int width_us, int rate_Hz, int count) {
    if (width_us <= 0 || rate_Hz <= 0 || count <= 0) {
        Serial.println("Error: Invalid pulse parameters.");
        return;
    }

    int period_us = 1000000 / rate_Hz;  // Convert Hz to microseconds per cycle
    int off_time = period_us - width_us; // Time LED remains OFF

    if (off_time < 0) {
        Serial.println("Error: Pulse width too large for given frequency.");
        return;
    }

    float totalTime = (count * period_us) / 1000000.0;  // Total estimated time in seconds

    Serial.print("Pulsing LED: ");
    Serial.print(count);
    Serial.print(" pulses, ");
    Serial.print(width_us);
    Serial.print("us width at ");
    Serial.print(rate_Hz);
    Serial.print(" Hz. Estimated duration: ");
    Serial.print(totalTime, 2);
    Serial.println(" seconds.");

    // Perform pulsing with real-time status
    for (int i = 0; i < count; i++) {
        digitalWrite(ctrlPin, LOW);  // Turn ON
        delayMicroseconds(width_us);
        digitalWrite(ctrlPin, HIGH);   // Turn OFF
        delayMicroseconds(off_time);

        // Show status every 10 pulses or at the last pulse
        if (i % 10 == 0 || i == count - 1) {
            float remainingTime = ((count - i - 1) * period_us) / 1000000.0;
            Serial.print("Pulse ");
            Serial.print(i + 1);
            Serial.print(" of ");
            Serial.print(count);
            Serial.print(" | Time left: ");
            Serial.print(remainingTime, 2);
            Serial.println(" seconds.");
        }
    }

    Serial.println("Pulse sequence completed.");
}

void loop() {
    if (Serial.available()) {
        String command = Serial.readStringUntil('\n'); // Read command
        command.trim(); // Remove unwanted spaces

        if (command.equalsIgnoreCase("ON")) {
            digitalWrite(ctrlPin, LOW);
            Serial.println("LED turned ON");
        } 
        else if (command.equalsIgnoreCase("OFF")) {
            digitalWrite(ctrlPin, HIGH);
            Serial.println("LED turned OFF");
        } 
        else if (command.startsWith("PWM")) {
            int duty = command.substring(4).toInt(); // Extract duty cycle value
            if (duty >= 0 && duty <= 100) {
                int pwmValue = map(duty, 0, 100, 0, 255); // Convert 0-100% to 8-bit PWM
                analogWrite(pwmPin, pwmValue);
                Serial.print("PWM set to ");
                Serial.print(duty);
                Serial.println("%");
            } else {
                Serial.println("Error: PWM value must be 0-100");
            }
        } 
        else if (command.startsWith("PULSE")) {
            int firstSpace = command.indexOf(' ');
            int secondSpace = command.indexOf(' ', firstSpace + 1);
            int thirdSpace = command.indexOf(' ', secondSpace + 1);

            if (firstSpace != -1 && secondSpace != -1 && thirdSpace != -1) {
                int width_us = command.substring(firstSpace + 1, secondSpace).toInt();
                int rate_Hz = command.substring(secondSpace + 1, thirdSpace).toInt();
                int count = command.substring(thirdSpace + 1).toInt();

                pulseLED(width_us, rate_Hz, count);
            } else {
                Serial.println("Invalid PULSE command. Use: PULSE <width_us> <rate_Hz> <count>");
            }
        }
        else if (command.equalsIgnoreCase("HELP")) {
            showHelp();
        }
        else if (command.equalsIgnoreCase("EXIT")) {
            Serial.println("Exiting program...");
            analogWrite(pwmPin, 0);
            digitalWrite(ctrlPin, HIGH);
        } 
        else {
            Serial.println("Unknown command. Type HELP for available commands.");
        }
    }
}
