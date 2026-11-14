// Kelvin cooling lab: DS18B20 waterproof temperature probe.
// Prints one line per second for Kelvin:  C,<milliseconds since start>,<temperature in C>
//
// Wiring (DS18B20 waterproof probe):
//   red    -> 5V
//   black  -> GND
//   yellow -> pin 2, plus a 4.7k resistor from pin 2 to 5V (the probe won't read without it)
//
// Libraries (Arduino IDE > Tools > Manage Libraries): "OneWire" by Paul Stoffregen,
// "DallasTemperature" by Miles Burton.
// Serial Monitor: 115200 baud. Close it before running Kelvin (only one program can use the port).

#include <OneWire.h>
#include <DallasTemperature.h>

const int PROBE_PIN = 2;          // change if the yellow wire is on another pin
const unsigned long PERIOD_MS = 1000;

OneWire oneWire(PROBE_PIN);
DallasTemperature probe(&oneWire);
unsigned long lastRequest = 0;

void setup() {
  Serial.begin(115200);
  probe.begin();
  probe.setResolution(12);              // 0.0625 C steps, needs up to 750 ms per reading
  probe.setWaitForConversion(false);    // ask for a reading, then come back for it, so timing stays steady
  if (probe.getDeviceCount() == 0) {
    Serial.println("# no DS18B20 found: check the 4.7k resistor and the yellow wire on pin 2");
  }
  probe.requestTemperatures();
  lastRequest = millis();
}

void loop() {
  unsigned long now = millis();
  if (now - lastRequest < PERIOD_MS) return;

  float c = probe.getTempCByIndex(0);   // the reading we asked for one second ago
  probe.requestTemperatures();          // start the next one
  lastRequest = now;

  if (c == DEVICE_DISCONNECTED_C || c < -50 || c > 125) {
    Serial.println("# probe not reading (disconnected or bad wire)");   // lines starting with # are notes, Kelvin ignores them
    return;
  }
  Serial.print("C,");
  Serial.print(now);
  Serial.print(",");
  Serial.println(c, 2);
}
