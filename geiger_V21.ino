#include <TinyGPS++.h>
#include <SoftwareSerial.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

static const int RXPin = 4, TXPin = 5;
static const uint32_t GPSBaud = 4800;
TinyGPSPlus gps;
SoftwareSerial ss(RXPin, TXPin);

// OLED 128×32
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 32
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// --- Geiger ---
volatile unsigned long counts = 0;
unsigned long previousMillis = 0;
const float uSv_per_CPM = 0.0027;

#define LOG_PERIOD 15000
#define NUM_SAMPLES 16

float samples[NUM_SAMPLES];
int sampleIndex = 0;
float total_uSv = 0;

// poprawne debounce dla tuby GM (50ms było absurdalnie duże!)
volatile unsigned long lastInterruptTime = 0;

void setup() {
  counts = 0;
  Serial.begin(1200);
  ss.begin(GPSBaud);

  pinMode(2, INPUT);
  attachInterrupt(digitalPinToInterrupt(2), impulse, FALLING);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("EROR OLED STOP"));
    for(;;);
  }

  display.display();
  delay(1000);

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println(F("Czekaj na GPS"));
  display.println(F("Szukam satelity"));
  display.println(F("Kalibracja Avg 4 min"));
  display.display();
  delay(1000);
}

void loop() {
  // stałe parsowanie GPS — nie ruszane!
  while (ss.available() > 0) {
    gps.encode(ss.read());
  }

  unsigned long currentMillis = millis();

  if (currentMillis - previousMillis > LOG_PERIOD) {
    previousMillis = currentMillis;

    // --- obliczenia Geigera ---
    unsigned long cpm = counts * (60000 / LOG_PERIOD);
    float uSv = cpm * uSv_per_CPM;

    total_uSv -= samples[sampleIndex];
    samples[sampleIndex] = uSv;
    total_uSv += samples[sampleIndex];
    sampleIndex = (sampleIndex + 1) % NUM_SAMPLES;

    float average_uSv = total_uSv / NUM_SAMPLES;

    // --- budowa ramki danych ---
    String dane;
    dane.reserve(200);

    String data = gps.date.isValid() ?
      String(gps.date.day()) + "." + String(gps.date.month()) + "." + String(gps.date.year()) + "r."
      : "00.00.0000r.";

    String czas = gps.time.isValid() ?
      String(gps.time.hour()) + ":" +
      (gps.time.minute() < 10 ? "0" : "") + String(gps.time.minute()) + ":" +
      (gps.time.second() < 10 ? "0" : "") + String(gps.time.second())
      : "00:00:00";

    String wspolrzedne = gps.location.isValid() ?
      String(gps.location.lat(), 6) + "|" + String(gps.location.lng(), 6)
      : "0.000000|0.000000";

    String wysokosc = gps.altitude.isValid() ?
      String(gps.altitude.meters(), 2)
      : "0.00";

    String satelity = gps.satellites.isValid() ?
      String(gps.satellites.value())
      : "0";

    String hdop = gps.hdop.isValid() ?
      String(gps.hdop.value() / 100.0, 2)
      : "0.00";

    String dokladnosc = gps.hdop.isValid() ?
      String(round(gps.hdop.value() * 3 / 100))
      : "0";

    dane.concat(data); dane.concat("|");
    dane.concat(czas); dane.concat("|");
    dane.concat(wspolrzedne); dane.concat("|");
    dane.concat(wysokosc); dane.concat("|");
    dane.concat(satelity); dane.concat("|");
    dane.concat(hdop); dane.concat("|");
    dane.concat(dokladnosc); dane.concat("|");
    dane.concat(String(uSv, 2)); dane.concat("|");
    dane.concat(String(average_uSv, 2));

    Serial.println(dane);

    // --- OLED ---
    display.clearDisplay();
    display.setCursor(0, 0);

    display.print(data);
    display.print(" ");
    display.println(czas);

    display.print(gps.location.lat(), 6);
    display.print(" ");
    display.println(gps.location.lng(), 6);

    display.print("S:");
    display.print(satelity);
    display.print(" H:");
    display.print(hdop);
    display.print(" D:");
    display.print(dokladnosc);
    display.println("m");

    display.print("uSv:");
    display.print(uSv, 2);
    display.print(" Avg:");
    display.println(average_uSv, 2);

    display.display();

    counts = 0;
  }
}

void impulse() {
  unsigned long t = micros();
  if (t - lastInterruptTime > 150) {   // realne filtrowanie szpilek tuby GM
    counts++;
    lastInterruptTime = t;
  }
}
