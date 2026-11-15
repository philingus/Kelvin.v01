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

#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>

const uint8_t ONE_WIRE_BUS = 2;
const uint8_t LED_PIN = 13;
const uint8_t BUZZER_PIN = 8;

const uint8_t RESOLUTION = 10;          // 10-bit: 0.25 C steps, ~188 ms conversion
const unsigned long SAMPLE_MS = 200;    // 5 samples per second

// Detection thresholds (tune tonight if needed)
const float RISE_TRIGGER_C  = 1.0;   // rise above recent minimum => probe is in water
const float BASELINE_BAND_C = 0.3;   // samples within this of the minimum count as "still in air"
const float DROP_TRIGGER_C  = 0.75;  // fall below in-water peak => probe was pulled out
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
  if (t < -100.0 || t == 85.0) return;

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
        state = IN_WATER;
        digitalWrite(LED_PIN, HIGH);
        Serial.print("E,IN,"); Serial.print(trial); Serial.print(","); Serial.println(insertMs);
      }
      break;
    }

    case IN_WATER:
      if (t >= peakTemp) {
        peakTemp = t;
        peakMs = ms;
      }

      if (cueEnabled && !cueFired && (ms - insertMs) / 1000.0 >= targetDwellS) {
        cueFired = true;
        tone(BUZZER_PIN, 2000, 200);
        Serial.print("E,CUE,"); Serial.print(trial); Serial.print(","); Serial.println(ms);
      }

      if (t < peakTemp - DROP_TRIGGER_C) {
        finishDip();
        digitalWrite(LED_PIN, LOW);
        state = IN_AIR;
        clearHist();
        pushHist(t, ms);
      }
      break;
  }
}

void setup() {
  Serial.begin(9600);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  sensors.begin();
  sensors.setResolution(RESOLUTION);
  sensors.setWaitForConversion(false);   // non-blocking so timing stays at 200 ms
  sensors.requestTemperatures();
  lastReqMs = millis();

  Serial.println("A,KELVIN_DIP_READY");
  Serial.println("time_ms,temp_c");
}

void loop() {
  readSerial();

  unsigned long now = millis();
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
