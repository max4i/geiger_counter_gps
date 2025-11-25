# 🚀 Geiger Counter - GPS Radiation Mapping System for Military Training

In the Polish Army known as **DOSIMETER**

Three mounting types allow attaching the Geiger Counter under TAROT, MARK4 drones and fixed-wing aircraft.
Weight approx. 300g.

![Project Banner](jpg/1.jpg)

Amateur radiation mapping system integrated with GPS technology and openmaps. 
Real-time environmental monitoring with GPS positioning and wireless data transmission.

## 📖 Project Description

The system combines Geiger-Müller tube, GPS module and wireless communication to create interactive radiation maps in real-time. Designed for environmental monitoring, scientific research, contamination reconnaissance and educational purposes.
Built using simple and inexpensive Geiger counter from AliExpress.

![Hardware Setup](jpg/13.jpg)

## ✨ Main Features

- **📡 Radiation Measurement** - Range 0.01-100 μSv/h with precise monitoring
- **🛰️ Precise GPS Positioning** - Real-time coordinates with OLED 128x32 or 128x64 display
- **📶 Two Data Transmission Options**:
  1. HC-12 (range up to 3 km)
  2. LoRa D02 (range up to 12 km)
- **🗺️ Real-time Mapping** - Interactive graphical interface
- **💾 Data Export** - CSV and KML formats for analysis
- **🎯 Automatic Map Generation** - Color-coded radiation levels

![Application Interface](jpg/2.jpg)

**System Calibration**

The system has been calibrated based on readings from the National Atomic Energy Agency (POLAND)
https://monitoring.paa.gov.pl/maps-portal/

## 🛠️ Hardware Components

### Basic Components
- **Arduino Nano** - Main controller
- **Geiger-Müller Tube** (radiationD cajoe)
- **GPS Module** - any NMEA 4800 speed
- **OLED Display** 128x32/64
- **Wireless Module** HC-12 or LoRa D02 1200 speed !!!
- **Power Supply** LiPo 3.7V
- **Powerbank module** (step up to 5V)
- **USB interface** for PC type CH340. Fixed speed 1200.

![Hardware Details](jpg/6.jpg)

### Connection Diagram
GM Tube → Pin 2 (INT)
GPS → Pins 4,5 Arduino Nano
OLED → I2C (A4,A5) HC-12/LoRa → UART

text

## 🚀 Quick Start

**Run Python Application**
```bash
cd python
python geiger_v21.py
Or Use Pre-built EXE File

Download from: Releases

Version 0.16 is the old version created for Air Force as a rationalization proposal

📡 Communication Protocol
Data Format

text
Date|Time|Latitude|Longitude|Altitude|Satellites|HDOP|Accuracy|Current_Dose|Average_Dose
Example Data Frame

text
24.11.2025r.|14:30:25|52.229770|21.011780|113.45|8|1.25|4|0.15|0.12
🗺️ Radiation Mapping
Application automatically generates maps with color-coded points:

🟢 Green: < 0.15 μSv/h (Safe - Normal background)

🟠 Orange: 0.15-1.0 μSv/h (Elevated - Further investigation required)

🔴 Red: > 1.0 μSv/h (Dangerous - Immediate action required)

🎯 Technical Specifications
Parameter	Specification
Measurement Range	0.01-100 μSv/h
GPS Accuracy	2-3 meters
Wireless Range	HC-12: 3km, LoRa: 12km
Update Frequency	15 seconds
Battery Life	4-6 hours
Display	OLED 128x32/64
Connectivity	GPS, Wireless 433MHz or 900MHz
📸 Gallery
https://jpg/7.jpg

https://jpg/12.jpg

https://jpg/3.jpg

https://jpg/10.jpg

👤 Author
max4i - Project and implementation

⚠️ SAFETY WARNING

This device is intended for educational and research purposes. Measurements do not replace professional radiation monitoring equipment. Always follow local radiation safety regulations and use certified equipment for safety-critical applications.

🔬 For scientific use, always calibrate with reference sources and maintain proper documentation.

⭐ If you find this project useful, please give it a star!
