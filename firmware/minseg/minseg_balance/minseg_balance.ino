#include <Wire.h>
#include <math.h>

// ============================================================
// MinSegMega V4.2 balancing, single NXT motor on M1
// Arduino Mega 2560. Based on Monika's balancing sketch.
//
// Serial Monitor: 115200 baud, line ending "Newline".
//
// For Kelvin, the board prints 20 times a second, always (even before calibrating):
//   B,time_ms,pitch,roll,angle,rate,pwm,armed,wheel_dps,calibrated
//     pitch = forward/back tilt from gravity (0 = straight up, + = toward batteries)
//     roll  = sideways tilt from gravity
//     angle = tilt relative to the calibrated upright (0 until calibrated)
// and once per calibration:
//   CAL,result,mean_pitch,mean_roll,pitch_spread,gyro_spread,gravity
//     result = ok or rejected
// Human messages start with "# ".
//
// One-letter commands (act immediately):
//   c = calibrate while held upright and still
//   a = arm while upright
//   x = stop motors
//   ? = print the current settings
//
// Tuning commands (letter + number, then Enter), no re-upload needed:
//   p8     angle gain Kp          (PWM per degree)
//   d0.8   tilt-rate gain Kd      (PWM per degree/second)
//   v0.1   wheel-speed gain Kv    (PWM per wheel degree/second), start at 0
//   w0.02  wheel-position gain Kw (PWM per wheel degree), start at 0
//   t1.5   balance trim in degrees (+ = lean toward batteries)
//   b40    dead-zone boost in PWM, start at 0
//   m200   max PWM (up to 255)
//
// Changed settings last until the board resets. When you find good
// numbers, write them into the defaults below and upload once.
// ============================================================

// -------------------- Pins --------------------
const byte MOTOR_A = 44;
const byte MOTOR_B = 45;
const byte LED_PIN = 49;
const byte ENC_A = 19;   // encoder 1, same as the speed sketch
const byte ENC_B = 18;

// -------------------- Direction --------------------
// Wheels must move TOWARD the direction the robot leans.
// Change this to -1 if that direction is reversed.
const int MOTOR_SIGN = -1;   // measured Nov 15: with 1 the wheels drove away from the lean

// While tilting toward the batteries, both angle and rate
// should be positive. Change to -1 if rate is reversed.
const float GYRO_SIGN = -1.0f;   // Monika measured: the rate reads reversed on our robot

// Encoder: on our robot, positive motor power makes the count go DOWN.
// This keeps wheel speed positive when the wheels drive the way a
// positive command drives them, whatever MOTOR_SIGN is.
const int ENC_SIGN = -1;
const float WHEEL_DEG_PER_COUNT = 360.0f / 720.0f;

// -------------------- Controller (defaults) --------------------
float Kp = 8.0f;       // PWM / degree
float Kd = 0.8f;       // PWM / (degree/second)
float Kv = 0.0f;       // PWM / (wheel degree/second)
float Kw = 0.0f;       // PWM / wheel degree
float trimDeg = 0.0f;  // + = lean slightly toward batteries
float deadBoost = 0.0f;
int maxPwm = 180;

// -------------------- Timing / filters --------------------
const uint32_t CONTROL_US = 5000;  // 200 Hz
const uint32_t MAX_DELAY_US = 20000;
const float ANGLE_FILTER_TAU = 0.35f;
const float RATE_FILTER_TAU = 0.015f;
const float WHEEL_FILTER_TAU = 0.05f;

// -------------------- Arming / shutdown --------------------
const float ARM_ANGLE_DEG = 8.0f;    // hand-held, so be forgiving
const float ARM_RATE_DPS = 25.0f;
const uint32_t ARM_STABLE_MS = 250;
const float FALL_ANGLE_DEG = 30.0f;

// -------------------- State --------------------
byte imuAddress;

bool calibrated = false;
bool armed = false;
bool fault = false;
bool stableWindow = false;

float uprightOffset = 0.0f;
float gyroBias = 0.0f;

float angleDeg = 0.0f;
float rawRateDps = 0.0f;
float filteredRateDps = 0.0f;
float commandPWM = 0.0f;
float accelMagnitude = 1.0f;
float pitchAbs = 0.0f;
float rollAbs = 0.0f;

volatile long encCount = 0;
long armCount = 0;
long lastWheelCount = 0;
float wheelDeg = 0.0f;
float wheelDps = 0.0f;

uint32_t lastControl;
uint32_t lastPrint;
uint32_t stableSince;

char cmdLetter = 0;
char cmdBuf[16];
byte cmdLen = 0;

struct Sample {
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
};

// -------------------- Encoder --------------------

void onEncA() { if (digitalRead(ENC_A) == digitalRead(ENC_B)) encCount++; else encCount--; }
void onEncB() { if (digitalRead(ENC_A) != digitalRead(ENC_B)) encCount++; else encCount--; }

long readCount() {
  noInterrupts();
  long c = encCount;
  interrupts();
  return c;
}

// -------------------- Motor functions --------------------

void stopMotor() {
  analogWrite(MOTOR_A, 0);
  analogWrite(MOTOR_B, 0);
  commandPWM = 0.0f;
}

void setMotor(float command) {
  command = constrain(command, -float(maxPwm), float(maxPwm));

  int pwm = (int)roundf(command) * MOTOR_SIGN;

  if (pwm > 0) {
    analogWrite(MOTOR_B, 0);
    analogWrite(MOTOR_A, pwm);
  } else if (pwm < 0) {
    analogWrite(MOTOR_A, 0);
    analogWrite(MOTOR_B, -pwm);
  } else {
    analogWrite(MOTOR_A, 0);
    analogWrite(MOTOR_B, 0);
  }
}

void disarm() {
  armed = false;
  stopMotor();
  digitalWrite(LED_PIN, LOW);
}

void tripFault(const __FlashStringHelper *message) {
  disarm();
  fault = true;
  stableWindow = false;
  Serial.print(F("# "));
  Serial.println(message);
  Serial.println(F("# Reset the board to clear this fault."));
}

// -------------------- MPU6050 functions --------------------

bool respondsAt(byte address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

bool writeRegister(byte reg, byte value) {
  Wire.beginTransmission(imuAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

int16_t readWord() {
  uint16_t hi = Wire.read();
  uint16_t lo = Wire.read();
  return (int16_t)((hi << 8) | lo);
}

bool readIMU(Sample &s) {
  Wire.beginTransmission(imuAddress);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;

  byte count = Wire.requestFrom(imuAddress, (byte)14);
  if (count != 14) {
    while (Wire.available()) Wire.read();
    return false;
  }

  s.ax = readWord();
  s.ay = readWord();
  s.az = readWord();
  readWord();  // temperature, unused
  s.gx = readWord();
  s.gy = readWord();
  s.gz = readWord();
  return true;
}

// Measured mounting: upright AY is about -1 g, tilting toward batteries raises AZ.
float getAccelAngle(const Sample &s) {
  return atan2f(float(s.az), -float(s.ay)) * 180.0f / PI;
}

// Sideways tilt: tipping toward the left or right side of the robot raises AX.
float getRollAngle(const Sample &s) {
  float ax = float(s.ax), ay = float(s.ay), az = float(s.az);
  return atan2f(ax, sqrtf(ay * ay + az * az)) * 180.0f / PI;
}

float getAccelMagnitude(const Sample &s) {
  float x = float(s.ax) / 16384.0f;
  float y = float(s.ay) / 16384.0f;
  float z = float(s.az) / 16384.0f;
  return sqrtf(x * x + y * y + z * z);
}

// -------------------- Status line for Kelvin --------------------

void printStatus() {
  // 20 times a second, without filling the serial transmit buffer.
  if (millis() - lastPrint < 50) return;
  lastPrint = millis();
  if (Serial.availableForWrite() < 63) return;
  Serial.print(F("B,"));
  Serial.print(millis());
  Serial.print(',');
  Serial.print(pitchAbs, 1);
  Serial.print(',');
  Serial.print(rollAbs, 1);
  Serial.print(',');
  Serial.print(calibrated ? angleDeg : 0.0f, 1);
  Serial.print(',');
  Serial.print(filteredRateDps, 1);
  Serial.print(',');
  Serial.print((int)commandPWM);
  Serial.print(',');
  Serial.print(armed ? 1 : 0);
  Serial.print(',');
  Serial.print(wheelDps, 0);
  Serial.print(',');
  Serial.println(calibrated ? 1 : 0);
}

// -------------------- Settings --------------------

void printSettings() {
  Serial.print(F("# Kp="));   Serial.print(Kp, 3);
  Serial.print(F(" Kd="));  Serial.print(Kd, 3);
  Serial.print(F(" Kv="));  Serial.print(Kv, 4);
  Serial.print(F(" Kw="));  Serial.print(Kw, 4);
  Serial.print(F(" trim=")); Serial.print(trimDeg, 2);
  Serial.print(F(" boost=")); Serial.print(deadBoost, 1);
  Serial.print(F(" max=")); Serial.print(maxPwm);
  Serial.print(F(" MOTOR_SIGN=")); Serial.print(MOTOR_SIGN);
  Serial.print(F(" GYRO_SIGN=")); Serial.println(GYRO_SIGN, 0);
}

void applySetting(char letter, float value) {
  switch (letter) {
    case 'p': Kp = value; break;
    case 'd': Kd = value; break;
    case 'v': Kv = value; break;
    case 'w': Kw = value; break;
    case 't': trimDeg = value; break;
    case 'b': deadBoost = constrain(value, 0.0f, 150.0f); break;
    case 'm': maxPwm = constrain((int)value, 0, 255); break;
  }
  printSettings();
}

// -------------------- Calibration --------------------

void calibrate() {
  disarm();
  calibrated = false;
  stableWindow = false;

  Serial.println(F("# Hold UPRIGHT and STILL."));
  Serial.println(F("# Calibration starts in 1 second."));
  delay(1000);

  const int N = 200;   // about 1 second of samples, so the whole calibration takes 2 seconds
  float angleSum = 0.0f, angleSquareSum = 0.0f;
  float gyroSum = 0.0f, gyroSquareSum = 0.0f;
  float magnitudeSum = 0.0f;
  float rollSum = 0.0f;

  for (int i = 0; i < N; i++) {
    Sample s;
    if (!readIMU(s)) {
      tripFault(F("FAULT: Sensor failed in calibration."));
      return;
    }
    float a = getAccelAngle(s);
    float g = float(s.gx) / 131.0f;
    angleSum += a;
    angleSquareSum += a * a;
    gyroSum += g;
    gyroSquareSum += g * g;
    magnitudeSum += getAccelMagnitude(s);
    rollSum += getRollAngle(s);
    delay(5);
  }

  float meanAngle = angleSum / N;
  float meanGyro = gyroSum / N;
  float meanMagnitude = magnitudeSum / N;
  float angleVariance = angleSquareSum / N - meanAngle * meanAngle;
  float gyroVariance = gyroSquareSum / N - meanGyro * meanGyro;
  float meanRoll = rollSum / N;

  bool rejected = fabsf(meanAngle) > 15.0f || angleVariance > 1.0f || gyroVariance > 4.0f ||
                  meanMagnitude < 0.85f || meanMagnitude > 1.15f;

  // One line for Kelvin with the numbers behind the decision.
  Serial.print(F("CAL,"));
  Serial.print(rejected ? F("rejected") : F("ok"));
  Serial.print(',');
  Serial.print(meanAngle, 2);
  Serial.print(',');
  Serial.print(meanRoll, 2);
  Serial.print(',');
  Serial.print(sqrtf(fabsf(angleVariance)), 2);
  Serial.print(',');
  Serial.print(sqrtf(fabsf(gyroVariance)), 2);
  Serial.print(',');
  Serial.println(meanMagnitude, 3);

  if (rejected) {
    Serial.println(F("# Calibration rejected: moved or not upright."));
    Serial.println(F("# Hold upright and still; send c again."));
    lastControl = micros();
    return;
  }

  uprightOffset = meanAngle;
  gyroBias = meanGyro;
  angleDeg = 0.0f;
  rawRateDps = 0.0f;
  filteredRateDps = 0.0f;
  accelMagnitude = meanMagnitude;
  calibrated = true;

  Serial.println(F("# Calibration OK. Motors OFF."));
  Serial.println(F("# Hold it upright, then send a."));

  lastControl = micros();
  lastPrint = millis();
}

// -------------------- Serial commands --------------------

void tryArm() {
  if (fault) {
    Serial.println(F("# Cannot arm: reset to clear fault."));
  } else if (!calibrated) {
    Serial.println(F("# Cannot arm: send c to calibrate first."));
  } else if (!stableWindow || millis() - stableSince < ARM_STABLE_MS ||
             (uint32_t)(micros() - lastControl) > MAX_DELAY_US) {
    Serial.println(F("# Cannot arm: hold upright and still, retry a."));
  } else {
    armCount = readCount();
    lastWheelCount = armCount;
    wheelDeg = 0.0f;
    wheelDps = 0.0f;
    armed = true;
    digitalWrite(LED_PIN, HIGH);
    Serial.println(F("# ARMED. x stops motors."));
  }
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c >= 'A' && c <= 'Z') c = c - 'A' + 'a';

    // Finish a tuning command at the end of the line.
    if (c == '\n' || c == '\r') {
      if (cmdLetter && cmdLen > 0) {
        cmdBuf[cmdLen] = 0;
        applySetting(cmdLetter, atof(cmdBuf));
      }
      cmdLetter = 0;
      cmdLen = 0;
      continue;
    }

    // Stop always works, even in the middle of a line.
    if (c == 'x') {
      disarm();
      cmdLetter = 0;
      cmdLen = 0;
      Serial.println(F("# STOPPED"));
      continue;
    }

    if (cmdLetter) {
      if (((c >= '0' && c <= '9') || c == '.' || c == '-') && cmdLen < sizeof(cmdBuf) - 1) {
        cmdBuf[cmdLen++] = c;
      }
      continue;
    }

    if (c == 'c') {
      if (fault) Serial.println(F("# Reset first to clear the fault."));
      else calibrate();
    } else if (c == 'a') {
      tryArm();
    } else if (c == '?') {
      printSettings();
    } else if (c == 'p' || c == 'd' || c == 'v' || c == 'w' || c == 't' || c == 'b' || c == 'm') {
      cmdLetter = c;
      cmdLen = 0;
    }
  }
}

// -------------------- Setup --------------------

void setup() {
  pinMode(MOTOR_A, OUTPUT);
  pinMode(MOTOR_B, OUTPUT);
  pinMode(LED_PIN, OUTPUT);

  // Unused second motor channel OFF.
  pinMode(2, OUTPUT);
  pinMode(46, OUTPUT);
  digitalWrite(2, LOW);
  digitalWrite(46, LOW);

  pinMode(ENC_A, INPUT_PULLUP);
  pinMode(ENC_B, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENC_A), onEncA, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_B), onEncB, CHANGE);

  disarm();
  Serial.begin(115200);

  Wire.begin();
  Wire.setClock(400000);
#ifdef WIRE_HAS_TIMEOUT
  Wire.setWireTimeout(3000, true);
#endif

  if (respondsAt(0x68)) {
    imuAddress = 0x68;
  } else if (respondsAt(0x69)) {
    imuAddress = 0x69;
  } else {
    tripFault(F("FAULT: MPU6050 not found."));
    return;
  }

  bool ok = writeRegister(0x6B, 0x01);
  ok &= writeRegister(0x1A, 0x03);  // sensor low-pass filter
  ok &= writeRegister(0x19, 0x04);  // 200 Hz
  ok &= writeRegister(0x1B, 0x00);  // gyro +/-250 deg/s
  ok &= writeRegister(0x1C, 0x00);  // accel +/-2 g
  if (!ok) {
    tripFault(F("FAULT: Sensor configuration failed."));
    return;
  }

  delay(100);
  Serial.println(F("# READY. Motors OFF."));
  Serial.println(F("# c = calibrate, a = arm, x = stop, ? = settings"));
  printSettings();

  lastControl = micros();
  lastPrint = millis();
}

// -------------------- Main loop --------------------

void loop() {
  handleSerial();

  if (fault) return;

  uint32_t now = micros();
  uint32_t elapsed = now - lastControl;
  if (elapsed < CONTROL_US) return;
  lastControl = now;

  if (elapsed > MAX_DELAY_US) {
    if (armed) {
      tripFault(F("FAULT: Control loop delayed."));
      return;
    }
    elapsed = CONTROL_US;  // not driving the motor, so just carry on
  }

  float dt = elapsed * 0.000001f;

  Sample s;
  if (!readIMU(s)) {
    tripFault(F("FAULT: IMU communication lost."));
    return;
  }

  // Live tilt from gravity, shown on Kelvin's dial even before calibrating.
  pitchAbs = getAccelAngle(s);
  rollAbs = getRollAngle(s);

  if (!calibrated) {
    stopMotor();
    printStatus();
    return;
  }

  float accelAngle = getAccelAngle(s) - uprightOffset;
  accelMagnitude = getAccelMagnitude(s);
  rawRateDps = GYRO_SIGN * (float(s.gx) / 131.0f - gyroBias);

  float rateWeight = dt / (RATE_FILTER_TAU + dt);
  filteredRateDps += rateWeight * (rawRateDps - filteredRateDps);

  float predictedAngle = angleDeg + rawRateDps * dt;
  if (accelMagnitude > 0.85f && accelMagnitude < 1.15f) {
    float alpha = ANGLE_FILTER_TAU / (ANGLE_FILTER_TAU + dt);
    angleDeg = alpha * predictedAngle + (1.0f - alpha) * accelAngle;
  } else {
    angleDeg = predictedAngle;
  }

  // Wheel position and speed since arming, in the motor command's direction.
  long count = readCount();
  float stepDeg = ENC_SIGN * MOTOR_SIGN * (count - lastWheelCount) * WHEEL_DEG_PER_COUNT;
  lastWheelCount = count;
  wheelDeg = ENC_SIGN * MOTOR_SIGN * (count - armCount) * WHEEL_DEG_PER_COUNT;
  float wheelWeight = dt / (WHEEL_FILTER_TAU + dt);
  wheelDps += wheelWeight * (stepDeg / dt - wheelDps);

  float error = angleDeg - trimDeg;

  bool nearUpright = fabsf(error) < ARM_ANGLE_DEG && fabsf(rawRateDps) < ARM_RATE_DPS &&
                     accelMagnitude > 0.9f && accelMagnitude < 1.1f;
  if (nearUpright) {
    if (!stableWindow) {
      stableWindow = true;
      stableSince = millis();
    }
  } else {
    stableWindow = false;
  }

  if (armed && fabsf(error) > FALL_ANGLE_DEG) {
    disarm();
    stableWindow = false;
    Serial.println(F("# STOPPED: Tilt exceeded 30 degrees."));
    Serial.println(F("# Support upright, then send a again."));
  }

  if (armed) {
    // Drive toward the lean. The wheel terms stop the slow drift:
    // they have the same sign as the angle terms on purpose.
    commandPWM = Kp * error + Kd * filteredRateDps + Kv * wheelDps + Kw * wheelDeg;
    if (commandPWM > 0.5f) commandPWM += deadBoost;
    else if (commandPWM < -0.5f) commandPWM -= deadBoost;
    commandPWM = constrain(commandPWM, -float(maxPwm), float(maxPwm));
    setMotor(commandPWM);
  } else {
    stopMotor();
  }

  printStatus();
}
