/*
  Homemade Tube Furnace Controller
  
  Required libraries (Arduino Library Manager):
    - Adafruit MAX31856
    - AutoPID  (by Ryan Downing — search "AutoPID")

  Wiring:
    MAX31856 VIN  -> 5V
    MAX31856 GND  -> GND
    MAX31856 SCK  -> Pin 13
    MAX31856 SDO  -> Pin 12 (MISO)
    MAX31856 SDI  -> Pin 11 (MOSI)
    MAX31856 CS   -> Pin 10
    MAX31856 FLT  -> Pin 9  (optional fault pin)
    SSR +         -> Pin 5
    SSR -         -> GND

  Serial commands (9600 baud):
    SETPOINT:xxx      set target °C   e.g. SETPOINT:800
    COOL              ramp down to room temperature
    STATUS            print current readings
    PIDSET:Kp,Ki,Kd   update gains    e.g. PIDSET:3.0,0.1,0.5

  TUNING:
    1. Start with PIDSET:1.0,0.0,0.0
    2. Raise Kp gradually until temp oscillates around setpoint
    3. Back Kp off ~30%
    4. Slowly raise Ki to remove steady-state error (droop below setpoint)
    5. Kd is often not needed for slow thermal systems — leave at 0 initially
*/

#include <Adafruit_MAX31856.h>
#include <AutoPID.h>
#include <SPI.h>

// ── Pins ─────────────────────────────────────────────────────────────────────
#define CS_PIN  10
#define FLT_PIN  9
#define SSR_PIN  5

// ── Settings — adjust these for your furnace ─────────────────────────────────
const double RAMP_PER_MIN   = 12.0;   // max °C per minute, heating or cooling
const double ROOM_TEMP      = 25.0;   // target when COOL is sent
const double MAX_SAFE_TEMP  = 1100.0; // SSR cut if temp exceeds this

const unsigned long RELAY_PULSE_MS = 5000; // SSR window in ms — don't change

// ── PID gains — tune these ───────────────────────────────────────────────────
double Kp = 0.5;
double Ki = 0.0;
double Kd = 0.0;

// ── Internal variables — do not edit below this line ─────────────────────────
double currentTemp    = 0.0;
double lastTemp       = 0.0;
double rampedSetpoint = 0.0;
double targetSetpoint = 0.0;
bool   relayState     = false;

unsigned long lastRampUpdate = 0;
unsigned long lastPrint      = 0;
bool heatingUp = true; // true = heating, false = cooling

void printStatusNow();
double printStatusNow(unsigned long diff, double lastTemp);

Adafruit_MAX31856 thermocouple = Adafruit_MAX31856(CS_PIN);
AutoPIDRelay furnacePID(&currentTemp, &rampedSetpoint, &relayState,
                         RELAY_PULSE_MS, Kp, Ki, Kd);

// ─────────────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(9600);
  pinMode(SSR_PIN, OUTPUT);
  digitalWrite(SSR_PIN, LOW);
  pinMode(FLT_PIN, INPUT);

  if (!thermocouple.begin()) {
    Serial.println("ERROR: MAX31856 not found. Check wiring.");
    while (1);
  }
  thermocouple.setThermocoupleType(MAX31856_TCTYPE_K);

  // Wait for a valid first reading
  Serial.println("Reading temperature...");
  delay(200);
  float t = thermocouple.readThermocoupleTemperature();
  while (isnan(t)) {
    delay(200);
    t = thermocouple.readThermocoupleTemperature();
  }
  currentTemp    = t;
  lastTemp       = t;
  rampedSetpoint = t;
  targetSetpoint = t;

  furnacePID.setTimeStep(1000);
  furnacePID.stop(); // don't run until user sends a command

  Serial.print("Welcome!");
  Serial.print("Ready. Temperature: ");
  Serial.print(currentTemp, 1);
  Serial.println(" C");
  Serial.println("Commands: SETPOINT:xxx | COOL | STATUS | PIDSET:Kp,Ki,Kd");
}

// ─────────────────────────────────────────────────────────────────────────────
void loop() {
  readTemp();
  checkSafety();
  handleSerial();
  updateRamp();
  furnacePID.run();
  if (!furnacePID.isStopped()) {
    digitalWrite(SSR_PIN, relayState ? HIGH : LOW);
  } else {
    digitalWrite(SSR_PIN, LOW);
  }
  printStatus();
}

// ── Temperature ───────────────────────────────────────────────────────────────
void readTemp() {
  float t = thermocouple.readThermocoupleTemperature();
  if (!isnan(t)) currentTemp = t;
}

// ── Safety cutoff ─────────────────────────────────────────────────────────────
void checkSafety() {
  if (millis() < 3000) return;
  if (digitalRead(FLT_PIN) == LOW) {
    furnacePID.stop();
    digitalWrite(SSR_PIN, LOW);
    Serial.println("!!! THERMOCOUPLE FAULT — SSR OFF. Check wiring. Reset to resume.");
    while (1);
  }
  if (currentTemp >= MAX_SAFE_TEMP) {
    furnacePID.stop();
    digitalWrite(SSR_PIN, LOW);
    Serial.println("!!! OVER-TEMPERATURE — SSR OFF. Reset Arduino to resume.");
    while (1); // halt
  }
}

// ── Ramp ──────────────────────────────────────────────────────────────────────
void updateRamp() {
  unsigned long now = millis();
  if (now - lastRampUpdate < 60000) return;
  lastRampUpdate = now;

  if (heatingUp) {
    if (rampedSetpoint < targetSetpoint) {
      rampedSetpoint = min(rampedSetpoint + RAMP_PER_MIN, targetSetpoint);
      Serial.print("Ramp -> "); Serial.print(rampedSetpoint, 1); Serial.println(" C");
    }
  } else {
    if (rampedSetpoint > targetSetpoint) {
      rampedSetpoint = max(rampedSetpoint - RAMP_PER_MIN, targetSetpoint);
      Serial.print("Ramp -> "); Serial.print(rampedSetpoint, 1); Serial.println(" C");
    }
  }
}

// ── Serial commands ───────────────────────────────────────────────────────────
void handleSerial() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  String upper = cmd;
  upper.toUpperCase();

  if (upper.startsWith("SETPOINT:")) {
    double sp = cmd.substring(9).toFloat();
    if (sp <= 0 || sp >= MAX_SAFE_TEMP) {
      Serial.println("ERROR: setpoint out of range.");
      return;
    }
    targetSetpoint = sp;
    heatingUp      = (sp >= currentTemp);
    // first ramp step immediately
    if (heatingUp)
      rampedSetpoint = min(currentTemp + RAMP_PER_MIN, sp);
    else
      rampedSetpoint = max(currentTemp - RAMP_PER_MIN, sp);
    lastRampUpdate = millis();
    furnacePID.setGains(Kp, Ki, Kd);
    furnacePID.reset();
    furnacePID.run();
    Serial.print(heatingUp ? "Heating to " : "Descending to ");
    Serial.print(sp, 1); Serial.println(" C");

  } else if (upper == "COOL") {
    targetSetpoint = ROOM_TEMP;
    heatingUp      = false;
    rampedSetpoint = max(currentTemp - RAMP_PER_MIN, ROOM_TEMP);
    lastRampUpdate = millis();
    furnacePID.setGains(Kp, Ki, Kd);
    furnacePID.reset();
    furnacePID.run();
    Serial.println("Cooling to room temperature.");

  } else if (upper == "STATUS") {
    printStatusNow();

  } else if (upper.startsWith("PIDSET:")) {
    String p  = cmd.substring(7);
    int    c1 = p.indexOf(',');
    int    c2 = p.lastIndexOf(',');
    if (c1 < 0 || c1 == c2) {
      Serial.println("ERROR: use PIDSET:Kp,Ki,Kd");
      return;
    }
    Kp = p.substring(0, c1).toFloat();
    Ki = p.substring(c1 + 1, c2).toFloat();
    Kd = p.substring(c2 + 1).toFloat();
    furnacePID.setGains(Kp, Ki, Kd);
    Serial.print("Gains set: Kp="); Serial.print(Kp);
    Serial.print(" Ki="); Serial.print(Ki);
    Serial.print(" Kd="); Serial.println(Kd);

  } else {
    Serial.print("Unknown: "); Serial.println(cmd);
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
void printStatus() {
  unsigned long diff = millis() - lastPrint;
  if (diff < 2000) return;
  lastPrint = millis();
  lastTemp = printStatusNow(diff, lastTemp);
}

void printStatusNow() {
  printStatusNow(millis() - lastPrint, lastTemp);
}

double printStatusNow(unsigned long diff, double lastTemp) {
  Serial.print("Temp: ");    Serial.print(currentTemp, 1);    Serial.print(" C | ");
  Serial.print("RampSP: ");  Serial.print(rampedSetpoint, 1); Serial.print(" C | ");
  Serial.print("Target: ");  Serial.print(targetSetpoint, 1); Serial.print(" C | ");
  Serial.print("SSR: ");     Serial.print(relayState ? "ON" : "OFF"); Serial.print(" | ");
  Serial.print("Rate: ");    Serial.print((currentTemp - lastTemp) / (double)diff * 60000.0, 1); Serial.print(" C/minute | ");
  Serial.println();
  return currentTemp;
}
