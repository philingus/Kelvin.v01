// Kelvin: MinSegMega V4.2 wheel speed controller (PI/PID), for the robot on a stand.
// Board: "Arduino Mega or Mega 2560". Serial Monitor: 115200 baud, "Newline".
// Power: the robot's own batteries, Battery switch ON and Driver switch ON. Nothing else plugged in.
//
// Prints 50 times a second:   T,time_ms,target_rpm,speed_rpm,motor_pwm,0
// Accepts (one per line):     K,kp,ki,kd      new controller settings (Kelvin sends these)
//                             S,rpm           new target speed in RPM (0 = stop)
//
// Measured on our robot (Nov 14): motor port 1 = D44/D45, encoder 1 = D19/D18, forward counts DOWN,
// full power ~130 RPM, and the motor needs ~40% power before it starts turning.

const int M1A = 44, M1B = 45;        // motor port 1
const int ENC_A = 19, ENC_B = 18;    // encoder 1 (both are interrupt pins)
const int DIR = -1;                  // forward counts down on our robot, so flip the sign
const float COUNTS_PER_REV = 720.0;  // both edges of both encoder channels
const unsigned long LOOP_MS = 20;    // 50 Hz
const int MAX_RPM = 120;             // refuse targets above this (full power is ~130)

volatile long enc = 0;
void onA() { if (digitalRead(ENC_A) == digitalRead(ENC_B)) enc++; else enc--; }
void onB() { if (digitalRead(ENC_A) != digitalRead(ENC_B)) enc++; else enc--; }

float kp = 4.0, ki = 10.0, kd = 0.0;  // good settings; Kelvin overrides these when it connects
float target = 0;
float integral = 0, lastError = 0, speedFilt = 0;
long lastCount = 0;
unsigned long lastLoop = 0;
char line[48];
int lineLen = 0;

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

void handleLine(char *s) {
  if (s[0] == 'K' && s[1] == ',') {
    float a, b, c;
    char *p = s + 2;
    a = atof(p); p = strchr(p, ','); if (!p) { Serial.println("# bad K line, use K,kp,ki,kd"); return; }
    b = atof(++p); p = strchr(p, ','); if (!p) { Serial.println("# bad K line, use K,kp,ki,kd"); return; }
    c = atof(++p);
    if (a < 0 || b < 0 || c < 0 || a > 200 || b > 200 || c > 20) { Serial.println("# K values out of range, ignored"); return; }
    if (b > 0) integral = integral * ki / b;   // keep ki*integral the same so a gain change doesn't jolt the wheel
    else integral = 0;
    kp = a; ki = b; kd = c;
    printSettings();
  } else if (s[0] == 'S' && s[1] == ',') {
    float t = atof(s + 2);
    if (t > MAX_RPM) t = MAX_RPM;
    if (t < -MAX_RPM) t = -MAX_RPM;
    target = t;
    if (target == 0) integral = 0;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(M1A, OUTPUT); pinMode(M1B, OUTPUT);
  pinMode(ENC_A, INPUT_PULLUP); pinMode(ENC_B, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENC_A), onA, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_B), onB, CHANGE);
  setMotor(0);
  Serial.println("# Kelvin MinSeg speed controller ready. Send S,60 to spin.");
  printSettings();
  lastLoop = millis();
}

void loop() {
  while (Serial.available()) {           // read commands without blocking
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) { line[lineLen] = 0; handleLine(line); lineLen = 0; }
    } else if (lineLen < (int)sizeof(line) - 1) {
      line[lineLen++] = c;
    }
  }

  unsigned long now = millis();
  if (now - lastLoop < LOOP_MS) return;
  float dt = (now - lastLoop) / 1000.0;
  lastLoop = now;

  noInterrupts(); long count = enc; interrupts();
  float rpm = DIR * (count - lastCount) / dt * 60.0 / COUNTS_PER_REV;
  lastCount = count;
  speedFilt = 0.7 * speedFilt + 0.3 * rpm;      // light smoothing of encoder steps

  float error = target - speedFilt;
  float deriv = (error - lastError) / dt;
  lastError = error;

  float out = kp * error + ki * integral + kd * deriv;
  // anti-windup: don't build the integral further while maxed out, but always let it unwind
  if ((out < 255 || error < 0) && (out > -255 || error > 0)) integral += error * dt;
  if (ki > 0) integral = constrain(integral, -255.0 / ki, 255.0 / ki);
  if (target == 0) { out = 0; integral = 0; }
  int pwm = (int)constrain(out, -255, 255);
  setMotor(pwm);

  Serial.print("T,"); Serial.print(now);
  Serial.print(","); Serial.print(target, 1);
  Serial.print(","); Serial.print(speedFilt, 1);
  Serial.print(","); Serial.print(pwm);
  Serial.println(",0");
}
