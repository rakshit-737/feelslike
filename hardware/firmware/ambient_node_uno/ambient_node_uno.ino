/*
 * ambient_node_uno.ino
 * --------------------
 * FeelsLike wired ambient-reference node: Arduino Uno + LM35.
 *
 * WHAT IT IS. A sensor-only node that measures the ROOM air around the
 * shoebox rig and streams it over USB serial. scripts/serial_bridge.py reads
 * each line and forwards it to the gateway. It never receives commands and
 * drives nothing, so it adds no actuation path and needs no watchdog.
 *
 * WHY WIRED. The ESP32 rig talks Wi-Fi/HTTP. This node talks a plain wired
 * serial link - the same shape as the RS-485/Modbus sensor buses real
 * buildings use - and reaches the same server-side seam.
 *
 * WIRE PROTOCOL (one JSON object per line, 115200 baud):
 *   {"node_id":"uno-ambient","temp_c":30.52,"counts":284.1,"seq":12,"uptime_s":24.0,"calibrated":false}
 *   {"node_id":"uno-ambient","fault":"sensor_open_or_short","counts":0.0,"seq":13,"uptime_s":26.0}
 * Lines starting with '#' are human-readable logs; the bridge ignores them.
 *
 * LM35 WIRING (flat face toward you, legs pointing down):
 *   left  = +Vs  -> Uno 5V
 *   middle = Vout -> Uno A0
 *   right = GND  -> Uno GND
 *
 * ACCURACY - read before trusting the number.
 *  - Reference: INTERNAL (~1.1 V on the ATmega328P) gives ~0.11 degC per ADC
 *    step for the LM35's 10 mV/degC, vs ~0.49 degC on the default 5 V
 *    reference, and it does not move with the USB supply voltage.
 *  - But the internal reference is only 1.0-1.2 V chip to chip: up to +/-10 %
 *    gain error, about +/-3 degC at 30 degC. This node MUST be cross-
 *    calibrated against the DHT22 (both sensors side by side, settled) before
 *    its readings are used. VREF_V below is the one knob that absorbs it.
 *  - 64-sample oversampling averages ADC noise between steps.
 *  - A disconnected A0 floats and can read plausible nonsense. The rail check
 *    below catches open/shorted wiring at the extremes, not every fault.
 */

const char NODE_ID[] = "uno-ambient";
const uint8_t LM35_PIN = A0;
const unsigned long PERIOD_MS = 2000UL;
const uint8_t OVERSAMPLE = 64;

// Nominal 1.1 V. After cross-calibration against the DHT22, set this to the
// value that makes the two sensors agree (hardware/README.md, ambient node).
const float VREF_V = 1.100;

// Flip to true ONLY after setting VREF_V from scripts/crosscal_ambient. Every
// reading carries it, and the dashboard labels the room temperature
// "uncalibrated" until it is true.
const bool CROSS_CALIBRATED = false;

unsigned long seq = 0;
unsigned long lastMs = 0;

void setup() {
  Serial.begin(115200);
  analogReference(INTERNAL);
  // The first conversions after switching reference are wrong: discard them.
  for (int i = 0; i < 10; i++) { analogRead(LM35_PIN); delay(10); }
  Serial.println(F("# ambient_node_uno: LM35 on A0, INTERNAL reference, 64x oversample"));
}

void loop() {
  unsigned long now = millis();
  if (now - lastMs < PERIOD_MS) return;
  lastMs = now;

  unsigned long sum = 0;
  for (uint8_t i = 0; i < OVERSAMPLE; i++) { sum += analogRead(LM35_PIN); delay(2); }
  float counts = (float)sum / OVERSAMPLE;

  Serial.print(F("{\"node_id\":\""));
  Serial.print(NODE_ID);
  Serial.print(F("\","));
  // Below ~2 degC or at full scale is not a room: report a fault, not a number.
  if (counts < 18.0 || counts > 1020.0) {
    Serial.print(F("\"fault\":\"sensor_open_or_short\","));
  } else {
    float tempC = counts * VREF_V / 1023.0 * 100.0;
    Serial.print(F("\"temp_c\":"));
    Serial.print(tempC, 2);
    Serial.print(F(","));
  }
  Serial.print(F("\"counts\":"));
  Serial.print(counts, 1);
  Serial.print(F(",\"seq\":"));
  Serial.print(seq++);
  Serial.print(F(",\"uptime_s\":"));
  Serial.print(now / 1000.0, 1);
  Serial.print(CROSS_CALIBRATED ? F(",\"calibrated\":true") : F(",\"calibrated\":false"));
  Serial.println(F("}"));
}
