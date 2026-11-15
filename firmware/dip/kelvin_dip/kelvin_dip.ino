// kelvin_dip.ino
// Kelvin: time constant lab (DS18B20 step response into hot water)
//
// Wiring (same as before):
//   Probe red    -> breadboard row 5 -> Uno 5V
//   Probe yellow -> breadboard row 7 -> Uno D2
//   Probe black  -> breadboard row 9 -> Uno GND
//   4.7k resistor between row 5 and row 7 (pull-up)
//   Optional piezo buzzer: + to D8, - to GND (for the timing cue)
//
// Libraries: OneWire (Paul Stoffregen), DallasTemperature (Miles Burton)
//
// Serial output @ 9600:
//   C,time_ms,temp_c                      every 200 ms (same format as before)
//   E,IN,trial,insert_ms                  probe went into the water
//   E,CUE,trial,ms                        target dwell reached (only if cue on)
//   D,trial,insert_ms,remove_ms,dwell_s,T_start,T_read,T_water,theta,tau_assumed,tau_actual,flags
//                                         one line per finished dip
//   A,...                                 acknowledgement of a command
//
// Serial commands (send with newline):
//   W,58.0   set water temperature (from a reference thermometer or a long dip)
//   T,4.0    set target dwell time in seconds (what the handout says)
//   B,1      turn the timing cue on (B,0 off)  <- this is Kelvin's "fix"
//   O,0.3    dwell offset correction in seconds (calibrate with a stopwatch)
//   R        re-measure room temperature (keep probe in air, dry)
//   N        reset trial counter
//
// Flags in the D line: OK, LATE, EARLY, NOT_RESET, NO_TWATER, SETTLED
//
// Version 2 (faster for live demos):
//   - "in" triggers on a 0.5 C rise instead of 1.0
//   - "out" triggers when a fast-rising reading stops rising for 1 second,
//     instead of waiting for it to fall 0.75 C in air (which took several seconds)
//   - prints A,NOPROBE when the sensor can't be read (loose wire)
//
// OPTIONAL water-contact wires (recommended: instant, exact in/out timing):
//   Jumper from Uno A0, taped along the probe so its metal tip sits beside the probe tip.
//   Jumper from Uno GND, with its metal tip resting in the water (tape it inside the cup).
//   The board switches to contact timing by itself the first time the A0 wire touches the water
//   (prints A,CONTACT,1). Without the wires it times dips from the temperature alone, which lags,
//   because the sensor inside the steel tube keeps warming after the probe leaves the water.

#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>

const uint8_t ONE_WIRE_BUS = 2;
const uint8_t LED_PIN = 13;
const uint8_t BUZZER_PIN = 8;
const uint8_t CONTACT_PIN = A0;         // optional water-contact wire (see top of file)
const int CONTACT_WET = 950;            // below this (of 1023) = the wire is in the water
const int CONTACT_DRY = 985;            // above this = the wire is out

const uint8_t RESOLUTION = 10;          // 10-bit: 0.25 C steps, ~188 ms conversion
const unsigned long SAMPLE_MS = 200;    // 5 samples per second

// Detection thresholds (tune tonight if needed)
const float RISE_TRIGGER_C  = 0.5;   // rise above recent minimum => probe is in water
const float BASELINE_BAND_C = 0.3;   // samples within this of the minimum count as "still in air"
const float DROP_TRIGGER_C  = 0.5;   // fall below in-water peak => probe was pulled out
const unsigned long STALL_MS = 1000; // a fast-rising reading that stops rising this long => pulled out
const float STRONG_RISE_C   = 0.75;  // "fast-rising" = rose at least this much in the last second
const unsigned long REARM_MS = 4000; // after a dip, wait for the reading to fall (or this long) before the next
const float NOT_RESET_C     = 1.5;   // dip started this far above room temp => not cooled back
const float TIMING_TOL_S    = 0.5;   // allowed error vs target dwell
const float SETTLED_THETA   = 0.995; // beyond this the one-point tau formula breaks down

float targetDwellS = 4.0;
float tWater = NAN;
float tRoom = NAN;
float dwellOffsetS = 0.0;
bool cueEnabled = false;

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);

enum State { WARMUP, IN_AIR, IN_WATER };
State state = WARMUP;

// Ring buffer of recent samples (about 1.2 s of history)
const uint8_t HIST = 6;
float histT[HIST];
unsigned long histMs[HIST];
uint8_t histCount = 0;
uint8_t histHead = 0;

// Warmup averaging for room temperature
float warmSum = 0;
uint8_t warmN = 0;
const uint8_t WARM_SAMPLES = 10;

// Current dip
int trial = 0;
unsigned long insertMs = 0;
float startTemp = 0;
float peakTemp = 0;
unsigned long peakMs = 0;
bool cueFired = false;
bool strongRise = false;        // the reading was rising fast at its latest new high
bool rearmed = true;            // ready to detect the next dip
float lastDipPeak = 0;
unsigned long lastDipEndMs = 0;
uint8_t readFails = 0;
bool contactMode = false;       // true once the contact wire has touched water
bool contactWet = false;
uint8_t wetCount = 0, dryCount = 0;
unsigned long lastContactMs = 0;
float lastTemp = NAN;

unsigned long lastReqMs = 0;
String cmdBuf = "";

void pushHist(float t, unsigned long ms) {
  histT[histHead] = t;
  histMs[histHead] = ms;
  histHead = (histHead + 1) % HIST;
  if (histCount < HIST) histCount++;
}

void clearHist() {
  histCount = 0;
  histHead = 0;
}

void printFloat(float v, uint8_t digits) {
  if (isnan(v)) Serial.print("nan");
  else Serial.print(v, digits);
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  char c = toupper(cmd.charAt(0));
  float val = 0;
  int comma = cmd.indexOf(',');
  if (comma >= 0) val = cmd.substring(comma + 1).toFloat();

  switch (c) {
    case 'W': tWater = val;            Serial.print("A,W,"); Serial.println(tWater, 2); break;
    case 'T': targetDwellS = val;      Serial.print("A,T,"); Serial.println(targetDwellS, 2); break;
    case 'B': cueEnabled = (val > 0.5); Serial.print("A,B,"); Serial.println(cueEnabled ? 1 : 0); break;
    case 'O': dwellOffsetS = val;      Serial.print("A,O,"); Serial.println(dwellOffsetS, 2); break;
    case 'R':
      state = WARMUP; warmSum = 0; warmN = 0; clearHist();
      Serial.println("A,R");
      break;
    case 'N': trial = 0; Serial.println("A,N"); break;
    default:  Serial.print("A,UNKNOWN,"); Serial.println(cmd); break;
  }
}

void readSerial() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (cmdBuf.length() > 0) handleCommand(cmdBuf);
      cmdBuf = "";
    } else if (cmdBuf.length() < 32) {
      cmdBuf += ch;
    }
  }
}

void finishDip() {
  float dwell = (peakMs - insertMs) / 1000.0 - dwellOffsetS;
  float tRead = peakTemp;

  String flags = "";
  if (!isnan(tRoom) && (startTemp - tRoom) > NOT_RESET_C) flags += "NOT_RESET|";
  if (dwell > targetDwellS + TIMING_TOL_S) flags += "LATE|";
  else if (dwell < targetDwellS - TIMING_TOL_S) flags += "EARLY|";

  float theta = NAN, tauAssumed = NAN, tauActual = NAN;
  if (isnan(tWater) || tWater <= startTemp) {
    flags += "NO_TWATER|";
  } else {
    theta = (tRead - startTemp) / (tWater - startTemp);
    if (theta <= 0.0 || theta >= SETTLED_THETA) {
      flags += "SETTLED|";
    } else {
      float lnTerm = log(1.0 - theta);
      tauAssumed = -targetDwellS / lnTerm;   // what the student calculates using the handout time
      tauActual  = -dwell / lnTerm;          // what it really is, using the measured time
    }
  }
  if (flags.length() == 0) flags = "OK";
  else flags.remove(flags.length() - 1);     // drop trailing |

  Serial.print("D,");
  Serial.print(trial);        Serial.print(",");
  Serial.print(insertMs);     Serial.print(",");
  Serial.print(peakMs);       Serial.print(",");
  printFloat(dwell, 2);       Serial.print(",");
  printFloat(startTemp, 2);   Serial.print(",");
  printFloat(tRead, 2);       Serial.print(",");
  printFloat(tWater, 2);      Serial.print(",");
  printFloat(theta, 3);       Serial.print(",");
  printFloat(tauAssumed, 2);  Serial.print(",");
  printFloat(tauActual, 2);   Serial.print(",");
  Serial.println(flags);
}

void processSample(float t, unsigned long ms) {
  // Ignore sensor error / power-on values
  if (t < -100.0 || t == 85.0) {
    if (++readFails >= 5) { readFails = 0; Serial.println("A,NOPROBE"); }
    return;
  }
  readFails = 0;
  lastTemp = t;

  Serial.print("C,");
  Serial.print(ms);
  Serial.print(",");
  Serial.println(t, 2);

  switch (state) {
    case WARMUP:
      warmSum += t;
      warmN++;
      if (warmN >= WARM_SAMPLES) {
        tRoom = warmSum / warmN;
        Serial.print("A,ROOM,"); Serial.println(tRoom, 2);
        state = IN_AIR;
        clearHist();
      }
      break;

    case IN_AIR: {
      if (contactMode) break;      // the contact wire decides when a dip starts
      // Right after a dip the reading can creep up a little more. Don't call that a new dip.
      if (!rearmed) {
        if (t <= lastDipPeak - DROP_TRIGGER_C || ms - lastDipEndMs >= REARM_MS) {
          rearmed = true;
          clearHist();
        } else {
          break;
        }
      }
      pushHist(t, ms);
      if (histCount < 2) break;

      // Recent minimum (handles a probe that is still cooling between dips)
      float minT = 1000;
      for (uint8_t i = 0; i < histCount; i++) if (histT[i] < minT) minT = histT[i];

      if (t - minT > RISE_TRIGGER_C) {
        // Insertion moment = newest sample that was still near the minimum
        unsigned long bestMs = 0;
        bool found = false;
        for (uint8_t i = 0; i < histCount; i++) {
          if (histT[i] <= minT + BASELINE_BAND_C && histMs[i] < ms) {
            if (!found || histMs[i] > bestMs) { bestMs = histMs[i]; found = true; }
          }
        }
        trial++;
        insertMs = found ? bestMs : ms;
        startTemp = minT;
        peakTemp = t;
        peakMs = ms;
        cueFired = false;
        strongRise = false;
        clearHist();
        state = IN_WATER;
        digitalWrite(LED_PIN, HIGH);
        Serial.print("E,IN,"); Serial.print(trial); Serial.print(","); Serial.println(insertMs);
      }
      break;
    }

    case IN_WATER: {
      // Rise over the last second (the ring holds about 1 s of in-water samples)
      float refTemp = (histCount < HIST) ? startTemp : histT[histHead];
      pushHist(t, ms);
      if (t > peakTemp) {
        strongRise = (t - refTemp) >= STRONG_RISE_C;
        peakTemp = t;
        peakMs = ms;
      }

      if (cueEnabled && !cueFired && (ms - insertMs) / 1000.0 >= targetDwellS) {
        cueFired = true;
        tone(BUZZER_PIN, 2000, 200);
        Serial.print("E,CUE,"); Serial.print(trial); Serial.print(","); Serial.println(ms);
      }

      bool dropped = t <= peakTemp - DROP_TRIGGER_C;
      bool stalled = strongRise && (ms - peakMs) >= STALL_MS;
      if (!contactMode && (dropped || stalled)) {
        finishDip();
        digitalWrite(LED_PIN, LOW);
        state = IN_AIR;
        rearmed = false;
        lastDipPeak = peakTemp;
        lastDipEndMs = ms;
        clearHist();
      }
      break;
    }
  }
}

// Water-contact timing: called every 10 ms.
void readContact(unsigned long now) {
  int v = analogRead(CONTACT_PIN);
  if (v < CONTACT_WET) { if (wetCount < 250) wetCount++; dryCount = 0; }
  else if (v > CONTACT_DRY) { if (dryCount < 250) dryCount++; wetCount = 0; }

  if (!contactWet && wetCount >= 3) {              // 30 ms in the water
    contactWet = true;
    if (!contactMode) { contactMode = true; Serial.println("A,CONTACT,1"); }
    if (state != IN_WATER && !isnan(lastTemp)) {
      if (state == WARMUP) { tRoom = lastTemp; Serial.print("A,ROOM,"); Serial.println(tRoom, 2); }
      trial++;
      insertMs = now - 30;
      startTemp = lastTemp;
      peakTemp = lastTemp;
      peakMs = now;
      cueFired = false;
      strongRise = false;
      clearHist();
      state = IN_WATER;
      digitalWrite(LED_PIN, HIGH);
      Serial.print("E,IN,"); Serial.print(trial); Serial.print(","); Serial.println(insertMs);
    }
  } else if (contactWet && dryCount >= 10) {       // 100 ms out of the water (ignores splashes)
    contactWet = false;
    if (state == IN_WATER) {
      peakTemp = lastTemp;                         // the reading at the moment it left the water
      peakMs = now - 100;
      finishDip();
      digitalWrite(LED_PIN, LOW);
      state = IN_AIR;
      rearmed = true;
      clearHist();
    }
  }
}

void setup() {
  Serial.begin(9600);
  pinMode(CONTACT_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  sensors.begin();
  sensors.setResolution(RESOLUTION);
  sensors.setWaitForConversion(false);   // non-blocking so timing stays at 200 ms
  sensors.requestTemperatures();
  lastReqMs = millis();

  Serial.println("A,KELVIN_DIP_READY,2");
  Serial.println("time_ms,temp_c");
}

void loop() {
  readSerial();

  unsigned long now = millis();
  if (now - lastContactMs >= 10) {
    lastContactMs = now;
    readContact(now);
    // fire the timing cue on time, not on the next temperature sample
    if (state == IN_WATER && cueEnabled && !cueFired && (now - insertMs) / 1000.0 >= targetDwellS) {
      cueFired = true;
      tone(BUZZER_PIN, 2000, 200);
      Serial.print("E,CUE,"); Serial.print(trial); Serial.print(","); Serial.println(now);
    }
  }
  if (now - lastReqMs >= SAMPLE_MS) {
    float t = sensors.getTempCByIndex(0);
    sensors.requestTemperatures();
    lastReqMs = now;
    processSample(t, now);
  }

  // Blink LED while cue is firing so it's visible from across the table
  if (state == IN_WATER && cueFired) {
    digitalWrite(LED_PIN, (millis() / 150) % 2);
  }
}
