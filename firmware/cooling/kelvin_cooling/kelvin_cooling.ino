#include <OneWire.h>
#include <DallasTemperature.h>

const int PROBE_PIN = A0;
const unsigned long PERIOD_MS = 1000;

OneWire oneWire(PROBE_PIN);
DallasTemperature probe(&oneWire);
unsigned long lastRequest = 0;

void setup() {
  Serial.begin(115200);
  probe.begin();
  probe.setResolution(12);
  probe.setWaitForConversion(false);
  if (probe.getDeviceCount() == 0) Serial.println("# no DS18B20 found");
  probe.requestTemperatures();
  lastRequest = millis();
}

void loop() {
  unsigned long now = millis();
  if (now - lastRequest < PERIOD_MS) return;
  float c = probe.getTempCByIndex(0);
  probe.requestTemperatures();
  lastRequest = now;
  if (c == DEVICE_DISCONNECTED_C || c < -50 || c > 125) {
    Serial.println("# probe not reading");
    return;
  }
  Serial.print("C,"); Serial.print(now); Serial.print(","); Serial.println(c, 2);
}
