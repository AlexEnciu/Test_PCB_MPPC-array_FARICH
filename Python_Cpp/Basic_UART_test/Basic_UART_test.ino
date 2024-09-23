void setup() {
  // Initialize Serial1 for communication with C11204-01 (Pins 18, 19)
  Serial1.begin(38400, SERIAL_8E1);  // C11204-01 uses 38400 baud rate
  
  // Initialize Serial0 (USB) for communication with the PC (for debugging)
  Serial.begin(9600);    // Serial monitor baud rate
  
  // Initial setup message
  Serial.println("Starting power supply control...");
  
  // Send the HOF command to turn off high voltage at the start
  sendOffCommand();
  delay(5000);  // Wait 5 seconds for the response
  
  // Send the HON command to turn on high voltage
  sendOnCommand();
  delay(5000);  // Wait another 5 seconds to check the response
}

void loop() {
  // Continuously check if C11204-01 has sent any data back
  if (Serial1.available()) {
    while (Serial1.available()) {
      char incomingByte = Serial1.read();
      Serial.print(incomingByte);  // Display received data on Serial Monitor
    }
    Serial.println();  // Print newline after response
  }
  
  // Optional: Send commands again after long delays (10 seconds between toggles)
  delay(10000);
  sendOffCommand();
  Serial.println("Sent HOF command (Turn OFF high voltage)");

  delay(10000);
  sendOnCommand();
  Serial.println("Sent HON command (Turn ON high voltage)");
}

// Function to send the "HON" (high voltage ON) command
void sendOnCommand() {
  // HON command with correct checksum (0x00E0)
  char honCommand[] = { 0x02, 'H', 'O', 'N', 0x03,0x00,0xE0, 0x0D };
  Serial1.write(honCommand, sizeof(honCommand));
}

// Function to send the "HOF" (high voltage OFF) command
void sendOffCommand() {
  // HOF command with correct checksum (0x00E2)
  char hofCommand[] = { 0x02, 'H', 'O', 'F', 0x03,0x00,0xE2, 0x0D };
  Serial1.write(hofCommand, sizeof(hofCommand));
}
