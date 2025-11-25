# 🚀 Licznik Geigera - System Mapowania Promieniowania z GPS przeznaczony do szklenia WOT.
W Polskiej armii nazywany jako DOZYMETR
Trzy rodzaje mocowania pozwalają na podwieszanie Licznika pod drony typu TAROT, MARK4 i płatowce.
Waga ok 300g.


![Baner Projektu](jpg/1.jpg)

Amatorski system do mapowania promieniowania zintegrowany z technologią GPS oraz openmaps. 
Monitoring środowiska w czasie rzeczywistym z pozycjonowaniem GPS i transmisją bezprzewodową.

## 📖 Opis Projektu

System łączy licznik Geigera-Müllera, moduł GPS i komunikację bezprzewodową do tworzenia interaktywnych map promieniowania w czasie rzeczywistym. Zaprojektowany do monitorowania środowiska, badań naukowych, rozpoznawania skażeń  i celów edukacyjnych.
do budowy wykorzystano prosty i tani licznik z aliexpress.

![Konfiguracja Sprzętu](jpg/13.jpg)

## ✨ Główne Funkcje

- **📡 Pomiar Promieniowania** - Zakres 0.01-100 μSv/h z precyzyjnym monitoringiem
- **🛰️ Precyzyjne Pozycjonowanie GPS** - Współrzędne w czasie rzeczywistym z wyświetlaczem OLED 128/32 lub 128/64
- **📶 dwie możliwości przesyłania danych **:
  1. HC-12 (zasięg do 3 km)
  2. LoRa D02 (zasięg do 12 km)
- **🗺️ Mapowanie w Czasie Rzeczywistym** - Interaktywny interfejs graficzny
- **💾 Eksport Danych** - Formaty CSV i KML do analizy
- **🎯 Automatyczne Generowanie Map** - Poziomy promieniowania oznaczone kolorami

![Interfejs Aplikacji](jpg/2.jpg)

**Kalibracja systemu**

System został skalibrowany  na podstawie wskazań Państwowej Agencji Atomistyki (POLAND)
https://monitoring.paa.gov.pl/maps-portal/

## 🛠️ Komponenty Sprzętowe

### Podstawowe Komponenty
- **Arduino Nano** - Główny kontroler
- **Tuba Geigera-Müllera** (radiationD cajoe)
- **Moduł GPS** dowolny nmea 4800 speed
- **Wyświetlacz OLED** 128x32/64
- **Moduł Bezprzewodowy** HC-12 lub LoRa D02 1200 speed !!!
- **Zasilanie** LiPo 3.7V
- **Powerbank module(step up 5V**
- interfejs USB pod PC typ ch340. stawiony na stałe z prędkością 1200.
(jpg/6.jpg)

### Schemat Podłączenia
GM → Pin 2 (INT) 
GPS → Piny 4,5 Arduino Nano
OLED → I2C (A4,A5) HC-12/LoRa → UART

**Uruchomienie Aplikacji Python**
cd python
python geiger_v21.py

Lub Użyj Gotowego Pliku EXE

(https://github.com/max4i/geiger_counter_gps/releases)

wersja 0.16 to stara wersja robiona dla  sił powietrznych jako wniosek racjonalizatorski

**Format Danych**
Data|Czas|Szerokość|Długość|Wysokość|Satelity|HDOP|Dokładność|Dawka_Chwilowa|Dawka_Uśredniona

**Przykładowa Ramka Danych**
24.11.2025r.|14:30:25|52.229770|21.011780|113.45|8|1.25|4|0.15|0.12

**Mapowanie Promieniowania**
Aplikacja automatycznie generuje mapy z kolorowymi punktami:

🟢 Zielony: < 0.15 μSv/h (Bezpieczne - Normalne tło)

🟠 Pomarańczowy: 0.15-1.0 μSv/h (Podwyższone - Wymaga dalszych badań)

🔴 Czerwony: > 1.0 μSv/h (Niebezpieczne - Wymaga natychmiastowego działania)

**Specyfikacja Techniczna**

Zakres Pomiarowy	0.01-100 μSv/h
Dokładność GPS	2-3 metry
Zasięg Bezprzewodowy	HC-12: 3km, LoRa: 12km
Częstotliwość Aktualizacji	15 sekund
Czas Pracy Baterii	4-6 godzin
Wyświetlacz	OLED 128x32/64
Łączność	z GS Bezprzewodowa $33Mhz lyb 900MHZ

**Autor**
max4i - Projekt i implementacja

**⚠️ OSTRZEŻENIE BEZPIECZEŃSTWA**

Urządzenie jest przeznaczone do celów edukacyjnych i badawczych. Pomiary nie zastępują profesjonalnego sprzętu do monitorowania promieniowania. Zawsze przestrzegaj lokalnych przepisów bezpieczeństwa radiacyjnego i używaj certyfikowanego sprzętu do zastosowań krytycznych dla bezpieczeństwa.

(jpg/7.jpg)

(jpg/12.jpg)

(jpg/3.jpg)

(jpg/10.jpg)
