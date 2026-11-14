// Kelvin: MinSegMega V4.2 wheel speed controller
// Board in Arduino IDE: "Arduino Mega or Mega 2560". Serial Monitor: 115200 baud, "Newline".
//
// Prints 50 times a second:   T,time_ms,target,speed,motor,tilt
// Accepts (one per line):     K,kp,ki,kd      change controller settings
//                             S,target        change target speed (0 = stop)
// Lines starting with '#' are info for humans; the backend ignores them.
//
// Pins from the MinSegMega V4.2 sheet. Motor 1 = M1A D44, M1B D45. Encoder 1 = D19, D18.
// If the wheel spins the wrong way or speed reads negative, swap ENC_A and ENC_B.

const int M1A = 44;      // motor 1, PWM
const int M1B = 45;      // motor 1, PWM
const int ENC_A = 19;    // encoder 1, interrupt pin
const int ENC_B = 18;    // encoder 1, interrupt pin

const unsigned long LOOP_MS = 20;   // 50 Hz
const float SPEED_SCALE = 0.1;      // speed = encoder counts per second * 0.1, keeps numbers readable

volatile long encCount = 0;

float kp = 1.0, ki = 2.0, kd = 0.0; // starting settings, tune these
float target = 0;
float integral = 0, lastError = 0, speedFilt = 0;
long lastCount = 0;
unsigned long lastLoop = 0;
String inLine = "";

void onEncA() {
  // quadrature: compare A and B to get direction
  if (digitalRead(ENC_A) == digitalRead(ENC_B)) encCount++; else encCount--;
}

void setMotor(int pwm) {
  pwm = constrain(pwm, -255, 255);
  if (pwm >= 0) { analogWrite(M1A, pwm); analogWrite(M1B, 0); }
  else          { analogWrite(M1A, 0);   analogWrite(M1B, -pwm); }
}

void printSettings() {
  Serial.print("# settings kp="); Serial.print(kp, 3);
  Serial.print(" ki="); Serial.print(ki, 3);
  Serial.print(" kd="); Serial.print(kd, 3);
  Serial.print(" target="); Serial.println(target, 1);
}

void handleLine(String s) {
  s.trim();
  if (s.length() < 2) return;
  char cmd = s.charAt(0);
  s = s.substring(2);                       // drop "K," or "S,"
  if (cmd == 'K') {
    int a = s.indexOf(','), b = s.indexOf(',', a + 1);
    if (a < 0 || b < 0) { Serial.println("# bad K line, use K,kp,ki,kd"); return; }
    kp = s.substring(0, a).toFloat();
    ki = s.substring(a + 1, b).toFloat();
    kd = s.substring(b + 1).toFloat();
    integral = 0;                           // fresh start with new settings
    printSettings();
  } else if (cmd == 'S') {
    target = s.toFloat();
    if (target == 0) integral = 0;
    printSettings();
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(M1A, OUTPUT); pinMode(M1B, OUTPUT);
  pinMode(ENC_A, INPUT_PULLUP); pinMode(ENC_B, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENC_A), onEncA, CHANGE);
  setMotor(0);
  Serial.println("# Kelvin MinSeg speed controller ready");
  printSettings();
  lastLoop = millis();
}

void loop() {
  // read commands without blocking
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { if (inLine.length()) handleLine(inLine); inLine = ""; }
    else if (inLine.length() < 64) inLine += c;
  }

  unsigned long now = millis();
  if (now - lastLoop < LOOP_MS) return;
  float dt = (now - lastLoop) / 1000.0;
  lastLoop = now;

  noInterrupts(); long count = encCount; interrupts();
  float speed = (count - lastCount) / dt * SPEED_SCALE;
  lastCount = count;
  speedFilt = 0.7 * speedFilt + 0.3 * speed;   // light smoothing

  float error = target - speedFilt;
  float deriv = (error - lastError) / dt;
  lastError = error;

  float out = kp * error + ki * integral + kd * deriv;
  // only build up the integral when the motor isn't already maxed out (anti-windup)
  if (out > -255 && out < 255) integral += error * dt;
  if (target == 0) { out = 0; integral = 0; }
  int pwm = (int)constrain(out, -255, 255);
  setMotor(pwm);

  Serial.print("T,"); Serial.print(now);
  Serial.print(","); Serial.print(target, 1);
  Serial.print(","); Serial.print(speedFilt, 1);
  Serial.print(","); Serial.print(pwm);
  Serial.println(",0");                       // tilt not used yet
}
