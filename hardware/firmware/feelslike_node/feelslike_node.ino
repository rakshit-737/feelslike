/*
 * feelslike_node.ino
 * ------------------
 * FeelsLike hardware-in-the-loop node ("shoebox zone").
 * Target: ESP32 dev board (Arduino core for ESP32, tested against core 3.x).
 *
 * What it does:
 *   - Reads temperature (+ relative humidity) from a DHT22 or SHT31.
 *   - Every POLL_MS, POSTs a JSON reading to <GATEWAY>/api/hw/reading.
 *   - Applies the fan / heater commands returned in the 200 response.
 *
 * Wire protocol (FROZEN — do not rename fields):
 *   POST body:
 *     {"node_id":"shoebox-1","temp_c":<float>,"rh_pct":<float or null>,
 *      "seq":<uint32>,"uptime_s":<float>}
 *   200 response:
 *     {"ok":true,"fan":0|1|2,"heater":true|false,
 *      "watchdog_s":<float>,"poll_s":<float>}
 *
 * =====================================================================
 * SAFETY MODEL (firmware layer — see hardware/README.md for all layers)
 * =====================================================================
 *  1. BOOT-SAFE:  both actuators are driven OFF in setup() before WiFi,
 *     sensors, or anything else is touched.
 *  2. WATCHDOG:   if no successful response (HTTP 200 AND parseable JSON
 *     with ok==true) has been received for watchdog_s seconds (default
 *     10 s), fan AND heater are forced off and stay off until the next
 *     good response arrives.
 *  3. HEATER RULE: the heater pin is only ever driven HIGH when the
 *     LATEST good response said heater=true AND the watchdog is fresh.
 *     There is no other code path that raises the heater pin.
 *  4. WIFI LOSS:  on WiFi disconnect, actuators are forced off first,
 *     then reconnection is attempted.
 * The physical kill switch (safety layer 0) and the server-side heater
 * duty-cycle cap (layer 2) are outside this file; this file is layer 1.
 * =====================================================================
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>   // response parsing only; POST body is snprintf'd

// ------------------------------------------------------------------
// CONFIG — CHANGE-ME section
// ------------------------------------------------------------------
#define WIFI_SSID    "CHANGE_ME_SSID"        // <-- your WiFi network name
#define WIFI_PASS    "CHANGE_ME_PASSWORD"    // <-- your WiFi password
#define GATEWAY_URL  "http://CHANGE_ME_HOST:8000"  // <-- gateway base URL, no trailing slash

#define NODE_ID      "shoebox-1"

// Sensor selection: exactly one of the two lines below must be active.
#define SENSOR_DHT22
// #define SENSOR_SHT31

// ------------------------------------------------------------------
// Pins and timing
// ------------------------------------------------------------------
#define DHT_PIN         4      // DHT22 data (only used with SENSOR_DHT22)
#define I2C_SDA_PIN     21     // SHT31 SDA  (only used with SENSOR_SHT31)
#define I2C_SCL_PIN     22     // SHT31 SCL  (only used with SENSOR_SHT31)
#define FAN_PIN         16     // gate input of fan MOSFET module (PWM)
#define HEATER_PIN      17     // gate input of heater MOSFET module (on/off)

#define POLL_MS_DEFAULT     2000UL   // ms between readings/POSTs (server may override via poll_s)
#define WATCHDOG_S_DEFAULT  10.0f    // s without a good response before all-off (server may override)
#define HTTP_TIMEOUT_MS     1500     // keep well under the poll period

// Fan PWM: 25 kHz (above audible range), 8-bit resolution.
#define FAN_PWM_FREQ_HZ 25000
#define FAN_PWM_BITS    8
// Fan level -> duty (out of 255): 0 -> 0%, 1 -> 60%, 2 -> 100%.
static const uint8_t FAN_DUTY[3] = { 0, 153, 255 };

// ------------------------------------------------------------------
// Sensor libraries (compile-time selected)
// ------------------------------------------------------------------
#if defined(SENSOR_DHT22) && defined(SENSOR_SHT31)
#error "Define exactly one of SENSOR_DHT22 / SENSOR_SHT31"
#endif
#if !defined(SENSOR_DHT22) && !defined(SENSOR_SHT31)
#error "Define exactly one of SENSOR_DHT22 / SENSOR_SHT31"
#endif

#ifdef SENSOR_DHT22
#include <DHT.h>                 // "DHT sensor library" by Adafruit
DHT dht(DHT_PIN, DHT22);
#endif

#ifdef SENSOR_SHT31
#include <Wire.h>
#include <Adafruit_SHT31.h>      // "Adafruit SHT31" library
Adafruit_SHT31 sht31;
#endif

// ------------------------------------------------------------------
// State
// ------------------------------------------------------------------
static uint32_t seq              = 0;                  // POST sequence counter
static uint32_t lastPollMs       = 0;                  // last POST attempt
static uint32_t pollMs           = POLL_MS_DEFAULT;    // current poll period
static float    watchdogS        = WATCHDOG_S_DEFAULT; // current watchdog window
static uint32_t lastGoodMs       = 0;                  // millis() of last good response
static bool     haveGoodResponse = false;              // ever received one since boot / last trip
static bool     watchdogTripped  = false;              // for edge-triggered logging

// Last commands from a good response. Only APPLIED while watchdog is fresh.
static int  cmdFan    = 0;
static bool cmdHeater = false;

// ------------------------------------------------------------------
// Actuators
// ------------------------------------------------------------------
static void actuatorsAllOff() {
  ledcWrite(FAN_PIN, 0);
  digitalWrite(HEATER_PIN, LOW);
}

// SAFETY: the ONLY function that drives the actuator pins from commands.
// Heater HIGH requires cmdHeater==true AND a fresh watchdog — enforced here.
static void applyActuators(bool watchdogFresh) {
  if (!watchdogFresh) {
    actuatorsAllOff();
    return;
  }
  int fanLevel = cmdFan;
  if (fanLevel < 0) fanLevel = 0;   // defensive clamp; server envelope is 0-2
  if (fanLevel > 2) fanLevel = 2;
  ledcWrite(FAN_PIN, FAN_DUTY[fanLevel]);
  digitalWrite(HEATER_PIN, cmdHeater ? HIGH : LOW);
}

static bool isWatchdogFresh() {
  if (!haveGoodResponse) return false;   // stale at boot until first good response
  return (millis() - lastGoodMs) < (uint32_t)(watchdogS * 1000.0f);
}

// ------------------------------------------------------------------
// Sensor read. Returns false if temperature is unreadable.
// rhOut is set to NAN when humidity is unreadable (serialized as null).
// ------------------------------------------------------------------
static bool readSensor(float &tempOut, float &rhOut) {
#ifdef SENSOR_DHT22
  tempOut = dht.readTemperature();
  rhOut   = dht.readHumidity();
#endif
#ifdef SENSOR_SHT31
  tempOut = sht31.readTemperature();
  rhOut   = sht31.readHumidity();
#endif
  return !isnan(tempOut);
}

// ------------------------------------------------------------------
// WiFi
// ------------------------------------------------------------------
static void connectWiFi() {
  // SAFETY: never sit in a connect loop with actuators live.
  actuatorsAllOff();
  Serial.printf("[wifi] connecting to \"%s\"...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000UL) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] connected, ip=%s rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] connect timed out; will retry");
  }
}

// ------------------------------------------------------------------
// POST reading, parse response, update commands + watchdog timestamps.
// ------------------------------------------------------------------
static void postReadingAndApply(float tempC, float rhPct) {
  // Hand-rolled POST body (fewer deps than serializing with ArduinoJson).
  char body[192];
  char rhField[16];
  if (isnan(rhPct)) {
    snprintf(rhField, sizeof(rhField), "null");
  } else {
    snprintf(rhField, sizeof(rhField), "%.1f", rhPct);
  }
  snprintf(body, sizeof(body),
           "{\"node_id\":\"%s\",\"temp_c\":%.2f,\"rh_pct\":%s,"
           "\"seq\":%lu,\"uptime_s\":%.1f}",
           NODE_ID, tempC, rhField,
           (unsigned long)seq, millis() / 1000.0f);
  seq++;

  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(String(GATEWAY_URL) + "/api/hw/reading");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)body, strlen(body));

  if (code != 200) {
    Serial.printf("[post] seq=%lu -> HTTP %d (no command update)\n",
                  (unsigned long)(seq - 1), code);
    http.end();
    return;  // watchdog keeps counting; it will trip if this persists
  }

  String resp = http.getString();
  http.end();

  // Response is parsed with ArduinoJson (v7 API).
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err || doc["ok"] != true) {
    Serial.printf("[post] seq=%lu -> 200 but bad body (%s); ignored\n",
                  (unsigned long)(seq - 1), err ? err.c_str() : "ok!=true");
    return;  // not a "good response": watchdog keeps counting
  }

  // Good response: adopt commands and refresh the watchdog.
  cmdFan    = doc["fan"] | 0;
  cmdHeater = doc["heater"] | false;
  if (doc["watchdog_s"].is<float>()) {
    float w = doc["watchdog_s"].as<float>();
    if (w >= 1.0f && w <= 120.0f) watchdogS = w;   // sanity bounds
  }
  if (doc["poll_s"].is<float>()) {
    float p = doc["poll_s"].as<float>();
    if (p >= 0.5f && p <= 60.0f) pollMs = (uint32_t)(p * 1000.0f);
  }
  lastGoodMs       = millis();
  haveGoodResponse = true;
  if (watchdogTripped) {
    Serial.println("[wdog] recovered: good response received, commands re-enabled");
    watchdogTripped = false;
  }
  Serial.printf("[cmd ] fan=%d heater=%s watchdog_s=%.1f poll_ms=%lu\n",
                cmdFan, cmdHeater ? "true" : "false",
                watchdogS, (unsigned long)pollMs);
}

// ------------------------------------------------------------------
// setup / loop
// ------------------------------------------------------------------
void setup() {
  // SAFETY RULE 1: actuator pins to a safe state BEFORE anything else.
  pinMode(HEATER_PIN, OUTPUT);
  digitalWrite(HEATER_PIN, LOW);
  ledcAttach(FAN_PIN, FAN_PWM_FREQ_HZ, FAN_PWM_BITS);  // ESP32 core 3.x LEDC API
  ledcWrite(FAN_PIN, 0);

  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("[boot] feelslike_node starting, actuators forced OFF");

#ifdef SENSOR_DHT22
  dht.begin();
  Serial.println("[boot] sensor: DHT22 on GPIO 4");
#endif
#ifdef SENSOR_SHT31
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  if (!sht31.begin(0x44)) {   // default SHT31 I2C address
    Serial.println("[boot] SHT31 not found at 0x44 — check wiring");
  } else {
    Serial.println("[boot] sensor: SHT31 on I2C (SDA=21, SCL=22)");
  }
#endif

  connectWiFi();
}

void loop() {
  // SAFETY RULE 4: WiFi loss -> actuators off, then reconnect.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] disconnected — actuators OFF, reconnecting");
    connectWiFi();   // forces actuators off internally
  }

  // SAFETY RULE 2: watchdog is evaluated every loop pass, not only at
  // poll time, so a stale link cannot leave the heater on between polls.
  bool fresh = isWatchdogFresh();
  if (!fresh && haveGoodResponse && !watchdogTripped) {
    Serial.printf("[wdog] TRIP: no good response for > %.1f s — all actuators OFF\n",
                  watchdogS);
    watchdogTripped = true;
  }
  applyActuators(fresh);

  // Poll cycle: read sensor, POST, apply whatever came back.
  if (millis() - lastPollMs >= pollMs) {
    lastPollMs = millis();
    float tempC, rhPct;
    if (readSensor(tempC, rhPct)) {
      if (isnan(rhPct)) {
        Serial.printf("[read] temp_c=%.2f rh_pct=null\n", tempC);
      } else {
        Serial.printf("[read] temp_c=%.2f rh_pct=%.1f\n", tempC, rhPct);
      }
      if (WiFi.status() == WL_CONNECTED) {
        postReadingAndApply(tempC, rhPct);
        applyActuators(isWatchdogFresh());  // apply fresh commands immediately
      }
    } else {
      // No POST on a failed read: if the sensor stays dead, the watchdog
      // trips and the actuators go off. That is the intended behavior.
      Serial.println("[read] sensor read failed (temp NaN) — skipping POST");
    }
  }

  delay(20);  // light loop pacing; keeps watchdog checks responsive
}
