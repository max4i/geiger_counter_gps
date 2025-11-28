#include <TinyGPS++.h>
#include <SoftwareSerial.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

static const int RXPin = 4, TXPin = 5;
static const uint32_t GPSBaud = 9600;
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
const float uSv_per_CPM = 0.00347;

#define LOG_PERIOD 15000
#define NUM_SAMPLES 16

float samples[NUM_SAMPLES];
int sampleIndex = 0;
float total_uSv = 0;

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

    // --- FORMAT DANYCH dla Serial (Python) BEZ ZER WIODĄCYCH ---
    String data = "00.00.00";
    if (gps.date.isValid()) {
      data = String(gps.date.day() < 10 ? "0" : "") + String(gps.date.day()) + "." + 
             String(gps.date.month() < 10 ? "0" : "") + String(gps.date.month()) + "." + 
             String(gps.date.year()).substring(2);
    }

    String czas = "00:00:00";
    if (gps.time.isValid()) {
      czas = String(gps.time.hour() < 10 ? "0" : "") + String(gps.time.hour()) + ":" +
             String(gps.time.minute() < 10 ? "0" : "") + String(gps.time.minute()) + ":" +
             String(gps.time.second() < 10 ? "0" : "") + String(gps.time.second());
    }

    String latitude = "00.000000";
    String longitude = "00.000000";
    if (gps.location.isValid()) {
      latitude = String(gps.location.lat(), 6);
      longitude = String(gps.location.lng(), 6);
    }

    // BEZ ZER WIODĄCYCH - naturalny format
    String wysokosc = "0";
    if (gps.altitude.isValid()) {
      wysokosc = String((int)gps.altitude.meters());
    }

    String satelity = "0";
    if (gps.satellites.isValid()) {
      satelity = String(gps.satellites.value());
    }

    String hdop = "0";
    if (gps.hdop.isValid()) {
      hdop = String(gps.hdop.value() / 100.0, 1);
    }

    String dokladnosc = "0";
    if (gps.hdop.isValid()) {
      dokladnosc = String((int)(gps.hdop.value() * 3 / 100));
    }

    String current_dose = String(uSv, 2);
    String average_dose = String(average_uSv, 2);

    // WYSYŁANIE DANYCH DO SERIAL (Python) - BEZ ZER WIODĄCYCH
    Serial.print(data); Serial.print("|");
    Serial.print(czas); Serial.print("|");
    Serial.print(latitude); Serial.print("|");
    Serial.print(longitude); Serial.print("|");
    Serial.print(wysokosc); Serial.print("|");
    Serial.print(satelity); Serial.print("|");
    Serial.print(hdop); Serial.print("|");
    Serial.print(dokladnosc); Serial.print("|");
    Serial.print(current_dose); Serial.print("|");
    Serial.println(average_dose);

    // --- OLED - WYŚWIETLANIE W CZYTELNEJ FORMIE ---
    display.clearDisplay();
    display.setCursor(0, 0);

    // Linia 1: Data i czas
    display.print(data);
    display.print(" ");
    display.println(czas);

    // Linia 2: Współrzędne (skrócone dla czytelności)
    String lat_display = gps.location.isValid() ? String(gps.location.lat(), 4) : "00.0000";
    String lon_display = gps.location.isValid() ? String(gps.location.lng(), 4) : "00.0000";
    display.print(lat_display);
    display.print(" ");
    display.println(lon_display);

    // Linia 3: Parametry GPS
    display.print("S:");
    display.print(satelity);
    display.print(" H:");
    display.print(hdop);
    display.print(" A:");
    display.print(wysokosc);
    display.println("m");

    // Linia 4: Dawki promieniowania
    display.print("Now:");
    display.print(current_dose);
    display.print(" Avg:");
    display.println(average_dose);

    display.display();

    counts = 0;
  }
}

void impulse() {
  unsigned long t = micros();
  if (t - lastInterruptTime > 150) {
    counts++;
    lastInterruptTime = t;
  }
}