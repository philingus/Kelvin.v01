// Kelvin: MinSegMega hardware check. Run this BEFORE minseg_speed.
// Board: "Arduino Mega or Mega 2560". Serial Monitor: 115200 baud.
// Put the robot on a stand so the wheels are off the table, batteries ON.
//
// Every 8 seconds it runs motor 1 forward, stops, runs it backward, stops, and prints the
// encoder count the whole time. What you want to see:
//   - the wheel spins one way, then the other
//   - "count" goes UP while it says FORWARD and DOWN while it says BACKWARD
// If the count goes the wrong way: swap ENC_A and ENC_B in minseg_speed.ino.
// If the count stays at 0 but the wheel spins: the encoder is on other pins (try 2 and 3).
// If the wheel does not spin: check the batteries and power switch, then tell Claude what you see.

const int M_A = 44;      // motor 1
const int M_B = 45;
const int ENC_A = 19;    // encoder 1
const int ENC_B = 18;

volatile long count = 0;
void onEnc() { if (digitalRead(ENC_A) == digitalRead(ENC_B)) count++; else count--; }

void drive(int pwm) {
  if (pwm >= 0) { analogWrite(M_A, pwm); analogWrite(M_B, 0); }
  else          { analogWrite(M_A, 0);   analogWrite(M_B, -pwm); }
}

void phase(const char *name, int pwm, int ms) {
  drive(pwm);
  for (int t = 0; t < ms; t += 250) {
    delay(250);
    noInterrupts(); long c = count; interrupts();
    Serial.print(name); Serial.print("  count = "); Serial.println(c);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(M_A, OUTPUT); pinMode(M_B, OUTPUT);
  pinMode(ENC_A, INPUT_PULLUP); pinMode(ENC_B, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENC_A), onEnc, CHANGE);
  Serial.println("MinSeg hardware check starting in 2 seconds. Wheels off the table!");
  delay(2000);
}

void loop() {
  phase("FORWARD ", 150, 2000);
  phase("STOP    ", 0, 2000);
  phase("BACKWARD", -150, 2000);
  phase("STOP    ", 0, 2000);
}
