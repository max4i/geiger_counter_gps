import os
import sys
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
from PIL import Image, ImageTk
import serial
import threading
import serial.tools.list_ports
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import queue
import time
from dataclasses import dataclass
from typing import List, Tuple
import json
import webbrowser
import xml.etree.ElementTree as ET
import zipfile


# Funkcja do obsługi ścieżek zasobów dla PyInstaller
def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


# Tryb awaryjny jeśli folium nie jest dostępne
try:
    import folium
    from folium import Popup

    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False
    print("Folium nie jest zainstalowane. Mapa będzie wyłączona.")


@dataclass
class GeigerData:
    """Klasa do przechowywania danych z licznika Geigera"""
    date: str = "00.00.00"
    time: str = "00:00:00"
    latitude: str = "00.000000"
    longitude: str = "00.000000"
    altitude: str = "00000"
    satellites: str = "00"
    hdop: str = "00"
    accuracy: str = "00"
    current_dose: str = "0.00"
    average_dose: str = "0.00"
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class ModernSerialReaderApp:
    def __init__(self, root):
        self.root = root
        self.setup_config()
        self.setup_variables()
        self.setup_modern_ui()
        self.setup_plot()

        # Dane historyczne dla mapy
        self.historical_data: List[GeigerData] = []
        self.current_map_path = None
        self.map_update_job = None

        # Załaduj ostatni port
        self.load_last_port()

    def setup_config(self):
        """Konfiguracja stałych programu"""
        self.APP_TITLE = "🚀 Wer. 2.3 DRONE GPS GEIGER - 15LBOT"
        self.WINDOW_SIZE = "1200x800"
        self.MIN_WINDOW_SIZE = "1000x600"

        # Ustawienia komunikacji
        self.BAUDRATE = 1200
        self.SERIAL_TIMEOUT = 0.1

        # NOWE: Zwiększony zakres danych do 4 godzin
        self.HISTORY_HOURS = 4  # 4 godziny historii
        self.UPDATE_INTERVAL = 15  # sekundy
        self.MAX_DATA_POINTS = (self.HISTORY_HOURS * 3600) // self.UPDATE_INTERVAL  # 960 punktów

        # Ścieżki
        self.LOG_DIR = "C:/logi_geiger/"
        self.RESOURCE_DIR = "resources/"
        self.MAP_DIR = "C:/logi_geiger/maps/"
        self.CONFIG_FILE = "C:/logi_geiger/app_config.json"

        # Kolory stylu Windows
        self.COLORS = {
            'bg_light': '#f0f0f0',
            'bg_dark': '#2d2d30',
            'accent': '#007acc',
            'success': '#107c10',
            'warning': '#d83b01',
            'text': '#323130'
        }

        # Utwórz katalogi
        os.makedirs(self.LOG_DIR, exist_ok=True)
        os.makedirs(self.MAP_DIR, exist_ok=True)

    def setup_variables(self):
        """Inicjalizacja zmiennych programu"""
        self.serial_port = None
        self.read_thread = None
        self.reading_event = threading.Event()
        self.data_queue = queue.Queue()
        self.log_file = None
        self.log_filename = None

        # Dane aplikacji
        self.current_data = GeigerData()
        self.dose_history = []
        self.time_history = []  # NOWE: Przechowujemy czasy pomiarów
        self.last_port = ""
        self.auto_map_update = False

    def load_last_port(self):
        """Ładuje ostatnio używany port z pliku konfiguracyjnego"""
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.last_port = config.get('last_port', '')
        except Exception as e:
            print(f"Błąd ładowania konfiguracji: {e}")

    def save_last_port(self):
        """Zapisuje ostatnio używany port do pliku konfiguracyjnego"""
        try:
            config = {'last_port': self.last_port}
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Błąd zapisywania konfiguracji: {e}")

    def setup_modern_ui(self):
        """Inicjalizacja nowoczesnego interfejsu użytkownika"""
        self.root.title(self.APP_TITLE)
        self.root.geometry(self.WINDOW_SIZE)
        self.root.minsize(1000, 600)
        self.root.configure(bg=self.COLORS['bg_light'])

        # Styl nowoczesny
        self.setup_styles()

        # Tworzenie layoutu z panelem bocznym
        self.create_main_layout()

        # Rozpocznij przetwarzanie kolejki
        self.process_queue()

    def setup_styles(self):
        """Konfiguracja nowoczesnych stylów"""
        style = ttk.Style()
        style.theme_use('vista')

    def create_main_layout(self):
        """Tworzy główny layout z panelem bocznym i obszarem zawartości"""
        # Główny kontener
        main_container = ttk.Frame(self.root)
        main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Lewy panel (sterowanie)
        self.create_control_panel(main_container)

        # Prawy panel (dane i wykresy)
        self.create_content_panel(main_container)

    def create_control_panel(self, parent):
        """Lewy panel z kontrolkami"""
        control_frame = ttk.LabelFrame(parent, text=" Sterowanie ", padding=10)
        control_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        # Port COM
        ttk.Label(control_frame, text="Port COM:").pack(anchor=tk.W, pady=(0, 5))
        self.port_combobox = ttk.Combobox(control_frame, width=15)
        self.port_combobox.pack(fill=tk.X, pady=(0, 10))

        # Przyciski sterowania
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(fill=tk.X, pady=5)

        self.refresh_btn = ttk.Button(button_frame, text="Odśwież",
                                      command=self.refresh_ports)
        self.refresh_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.connect_btn = ttk.Button(button_frame, text="Połącz",
                                      command=self.connect_serial)
        self.connect_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.disconnect_btn = ttk.Button(control_frame, text="Rozłącz",
                                         command=self.disconnect_serial,
                                         state=tk.DISABLED)
        self.disconnect_btn.pack(fill=tk.X, pady=5)

        # Status
        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, pady=10)
        ttk.Label(status_frame, text="Status:").pack(anchor=tk.W)
        self.status_label = ttk.Label(status_frame, text="Niepołączono",
                                      foreground="red", font=('Segoe UI', 9, 'bold'))
        self.status_label.pack(anchor=tk.W)

        # Separator
        ttk.Separator(control_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # Szybkie akcje
        ttk.Label(control_frame, text="Szybkie akcje:", font=('Segoe UI', 9, 'bold')).pack(anchor=tk.W)

        self.map_btn = ttk.Button(control_frame, text="Generuj mapę",
                                  command=self.generate_and_show_map)
        self.map_btn.pack(fill=tk.X, pady=5)

        # DODANY PRZYCISK RESETU WYKRESU
        ttk.Button(control_frame, text="Resetuj wykres",
                   command=self.reset_plot).pack(fill=tk.X, pady=5)

        ttk.Button(control_frame, text="Otwórz folder logów",
                   command=self.open_log_folder).pack(fill=tk.X, pady=5)

        ttk.Button(control_frame, text="Eksportuj dane (CSV)",
                   command=self.export_data).pack(fill=tk.X, pady=5)

        ttk.Button(control_frame, text="Eksportuj dane (KML)",
                   command=self.export_kml).pack(fill=tk.X, pady=5)

        # Puste miejsce do wypełnienia
        empty_space = ttk.Frame(control_frame)
        empty_space.pack(fill=tk.BOTH, expand=True)

        # Logo na samym dole
        logo_frame = ttk.Frame(control_frame)
        logo_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)

        # Logo 2 - logo.jpg (NA GÓRZE)
        try:
            logo2_path = resource_path("logo.jpg")
            if os.path.exists(logo2_path):
                logo2_img = Image.open(logo2_path)
                logo2_img = logo2_img.convert("RGBA")
                logo2_data = logo2_img.getdata()

                new_data = []
                for item in logo2_data:
                    if item[0] > 240 and item[1] > 240 and item[2] > 240:
                        new_data.append((255, 255, 255, 0))
                    else:
                        new_data.append(item)

                logo2_img.putdata(new_data)
                logo2_img = logo2_img.resize((160, 160), Image.LANCZOS)
                self.logo2_photo = ImageTk.PhotoImage(logo2_img)
                logo2_label = tk.Label(logo_frame, image=self.logo2_photo, bg=self.COLORS['bg_light'])
                logo2_label.pack(pady=(0, 5))
        except Exception as e:
            print(f"Błąd ładowania logo2: {e}")

        # Logo 1 - 15lbot.jpg (POD LOGO.JPG)
        try:
            logo1_path = resource_path("15lbot.jpg")
            if os.path.exists(logo1_path):
                logo1_img = Image.open(logo1_path)
                logo1_img = logo1_img.convert("RGBA")
                logo1_data = logo1_img.getdata()

                new_data = []
                for item in logo1_data:
                    if item[0] > 240 and item[1] > 240 and item[2] > 240:
                        new_data.append((255, 255, 255, 0))
                    else:
                        new_data.append(item)

                logo1_img.putdata(new_data)
                logo1_img = logo1_img.resize((160, 160), Image.LANCZOS)
                self.logo1_photo = ImageTk.PhotoImage(logo1_img)
                logo1_label = tk.Label(logo_frame, image=self.logo1_photo, bg=self.COLORS['bg_light'])
                logo1_label.pack()
        except Exception as e:
            print(f"Błąd ładowania logo1: {e}")

        self.refresh_ports()

    def create_content_panel(self, parent):
        """Prawy panel z danymi, wykresami i mapą"""
        # Notebook (zakładki)
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Zakładka 1: Monitorowanie w czasie rzeczywistym
        self.create_monitoring_tab()

        # Zakładka 2: Mapa
        self.create_map_tab()

        # Zakładka 3: Logi
        self.create_logs_tab()

    def create_monitoring_tab(self):
        """Zakładka monitorowania"""
        monitor_tab = ttk.Frame(self.notebook)
        self.notebook.add(monitor_tab, text="Monitorowanie")

        # Górna sekcja - dane pomiarowe
        data_frame = ttk.LabelFrame(monitor_tab, text=" Dane pomiarowe ", padding=10)
        data_frame.pack(fill=tk.X, pady=(0, 10))

        # Siatka danych
        self.create_data_grid(data_frame)

        # Środkowa sekcja - wykres
        # NOWE: Zaktualizowany tytuł wykresu
        graph_frame = ttk.LabelFrame(monitor_tab, text=" Historia dawki - Ostatnie 4 godziny ", padding=10)
        graph_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Wykres będzie inicjalizowany w setup_plot()
        self.graph_container = ttk.Frame(graph_frame)
        self.graph_container.pack(fill=tk.BOTH, expand=True)

        # Dolna sekcja - statystyki
        stats_frame = ttk.LabelFrame(monitor_tab, text=" Statystyki ", padding=10)
        stats_frame.pack(fill=tk.X)

        self.create_stats_grid(stats_frame)

    def create_data_grid(self, parent):
        """Siatka z danymi pomiarowymi"""
        # Wiersz 1 - Dawki
        dose_frame = ttk.Frame(parent)
        dose_frame.pack(fill=tk.X, pady=5)

        self.current_dose_var = tk.StringVar(value="0.00 μSv")
        self.average_dose_var = tk.StringVar(value="0.00 μSv/h")

        ttk.Label(dose_frame, text="Dawka chwilowa:", font=('Segoe UI', 10, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(dose_frame, textvariable=self.current_dose_var, font=('Segoe UI', 12, 'bold'),
                  foreground="blue").pack(side=tk.LEFT, padx=(0, 30))

        ttk.Label(dose_frame, text="Dawka uśredniona:", font=('Segoe UI', 10, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(dose_frame, textvariable=self.average_dose_var, font=('Segoe UI', 24, 'bold'),
                  foreground="red").pack(side=tk.LEFT)

        # Wiersz 2 - Dane GPS
        gps_frame = ttk.Frame(parent)
        gps_frame.pack(fill=tk.X, pady=5)

        # 3 kolumny
        gps_frame.columnconfigure(0, weight=1)
        gps_frame.columnconfigure(1, weight=1)
        gps_frame.columnconfigure(2, weight=1)

        # Pozycja
        pos_frame = ttk.LabelFrame(gps_frame, text=" Pozycja ", padding=5)
        pos_frame.grid(row=0, column=0, padx=5, sticky="ew")

        self.lat_var = tk.StringVar(value="N: 00.000000")
        self.lon_var = tk.StringVar(value="E: 00.000000")
        ttk.Label(pos_frame, textvariable=self.lat_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(pos_frame, textvariable=self.lon_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

        # Czas
        time_frame = ttk.LabelFrame(gps_frame, text=" Czas ", padding=5)
        time_frame.grid(row=0, column=1, padx=5, sticky="ew")

        self.date_var = tk.StringVar(value="Data: 00.00.00")
        self.time_var = tk.StringVar(value="Czas: 00:00:00")
        ttk.Label(time_frame, textvariable=self.date_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(time_frame, textvariable=self.time_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

        # Jakość sygnału
        quality_frame = ttk.LabelFrame(gps_frame, text=" Jakość GPS ", padding=5)
        quality_frame.grid(row=0, column=2, padx=5, sticky="ew")

        self.sat_var = tk.StringVar(value="Satelity: 0")
        self.hdop_var = tk.StringVar(value="HDOP: 0.0")
        self.alt_var = tk.StringVar(value="Wysokość: 0 m")
        ttk.Label(quality_frame, textvariable=self.sat_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(quality_frame, textvariable=self.hdop_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(quality_frame, textvariable=self.alt_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

    def create_stats_grid(self, parent):
        """Siatka ze statystykami"""
        stats_frame = ttk.Frame(parent)
        stats_frame.pack(fill=tk.X, pady=5)

        # 4 kolumny
        for i in range(4):
            stats_frame.columnconfigure(i, weight=1)

        self.min_dose_var = tk.StringVar(value="Min: 0.00")
        self.max_dose_var = tk.StringVar(value="Max: 0.00")
        self.avg_dose_var = tk.StringVar(value="Średnia: 0.00")
        self.points_var = tk.StringVar(value="Punkty: 0")

        ttk.Label(stats_frame, textvariable=self.min_dose_var,
                  font=('Segoe UI', 9)).grid(row=0, column=0, padx=5)
        ttk.Label(stats_frame, textvariable=self.max_dose_var,
                  font=('Segoe UI', 9)).grid(row=0, column=1, padx=5)
        ttk.Label(stats_frame, textvariable=self.avg_dose_var,
                  font=('Segoe UI', 9)).grid(row=0, column=2, padx=5)
        ttk.Label(stats_frame, textvariable=self.points_var,
                  font=('Segoe UI', 9)).grid(row=0, column=3, padx=5)

    def create_map_tab(self):
        """Zakładka mapy z podglądem w czasie rzeczywistym"""
        self.map_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.map_tab, text="Mapa")

        # Kontrolki mapy
        map_control_frame = ttk.Frame(self.map_tab)
        map_control_frame.pack(fill=tk.X, pady=5)

        ttk.Button(map_control_frame, text="Generuj i pokaż mapę",
                   command=self.generate_and_show_map).pack(side=tk.LEFT, padx=5)
        ttk.Button(map_control_frame, text="Otwórz w przeglądarce",
                   command=self.open_map_in_browser).pack(side=tk.LEFT, padx=5)
        ttk.Button(map_control_frame, text="Odśwież podgląd",
                   command=self.refresh_map_preview).pack(side=tk.LEFT, padx=5)

        # Status mapy
        self.map_status_var = tk.StringVar(value="Kliknij 'Generuj i pokaż mapę'")
        ttk.Label(map_control_frame, textvariable=self.map_status_var,
                  font=('Segoe UI', 9)).pack(side=tk.RIGHT, padx=10)

        # Ramka z podglądem mapy
        map_preview_frame = ttk.LabelFrame(self.map_tab, text=" Podgląd mapy w czasie rzeczywistym ", padding=10)
        map_preview_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Kontrolki automatycznej aktualizacji
        auto_update_frame = ttk.Frame(map_preview_frame)
        auto_update_frame.pack(fill=tk.X, pady=5)

        self.auto_update_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(auto_update_frame, text="Automatyczna aktualizacja podglądu (co 15s)",
                        variable=self.auto_update_var,
                        command=self.toggle_auto_update).pack(side=tk.LEFT)

        # Obszar na podgląd mapy z kolorowym tekstem
        self.map_preview_text = tk.Text(
            map_preview_frame,
            wrap=tk.WORD,
            width=80,
            height=20,
            font=('Consolas', 9),
            bg='white'
        )

        # Scrollbar dla tekstu
        scrollbar = ttk.Scrollbar(map_preview_frame, orient=tk.VERTICAL, command=self.map_preview_text.yview)
        self.map_preview_text.configure(yscrollcommand=scrollbar.set)

        self.map_preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Konfiguracja kolorów tekstu
        self.map_preview_text.tag_configure("green", foreground="green")
        self.map_preview_text.tag_configure("orange", foreground="orange")
        self.map_preview_text.tag_configure("red", foreground="red")
        self.map_preview_text.tag_configure("blue", foreground="blue")
        self.map_preview_text.tag_configure("bold", font=('Consolas', 9, 'bold'))

        # Początkowa informacja
        initial_info = """🗺️ DANE MAPY POMIARÓW PROMIENIOWANIA - CZAS RZECZYWISTY

Aby zobaczyć mapę:
1. Połącz z urządzeniem i zbierz dane GPS
2. Kliknij 'Generuj i pokaż mapę'
3. Mapa zostanie wygenerowana i otwarta w przeglądarce
4. Tutaj zobaczysz informacje o punktów pomiarowych w czasie rzeczywistym

Kolory punktów na mapie:
• ZIELONY - dawka < 0.15 μSv/h
• POMARAŃCZOWY - dawka 0.15-1.0 μSv/h  
• CZERWONY - dawka > 1.0 μSv/h
• Linia - trasa pomiarów

Włącz 'Automatyczną aktualizację' aby na bieżąco śledzić nowe punkty!
"""
        self.map_preview_text.insert(tk.END, initial_info)

        # Kolorowanie tekstu
        self.map_preview_text.tag_add("green", "9.0", "9.1")
        self.map_preview_text.tag_add("green", "9.2", "9.9")
        self.map_preview_text.tag_add("orange", "10.0", "10.1")
        self.map_preview_text.tag_add("orange", "10.2", "10.13")
        self.map_preview_text.tag_add("red", "11.0", "11.1")
        self.map_preview_text.tag_add("red", "11.2", "11.9")
        self.map_preview_text.tag_add("blue", "12.0", "12.1")
        self.map_preview_text.tag_add("blue", "12.2", "12.7")

        self.map_preview_text.config(state=tk.DISABLED)

    def create_logs_tab(self):
        """Zakładka logów"""
        logs_tab = ttk.Frame(self.notebook)
        self.notebook.add(logs_tab, text="Logi")

        # Kontrolki logów
        log_control_frame = ttk.Frame(logs_tab)
        log_control_frame.pack(fill=tk.X, pady=5)

        ttk.Button(log_control_frame, text="Wyczyść logi",
                   command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_control_frame, text="Zapisz logi",
                   command=self.save_logs).pack(side=tk.LEFT, padx=5)

        # Obszar tekstowy
        self.log_text = scrolledtext.ScrolledText(
            logs_tab,
            wrap=tk.WORD,
            width=80,
            height=20,
            font=('Consolas', 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def setup_plot(self):
        """Inicjalizacja wykresu matplotlib"""
        self.fig, self.ax = plt.subplots(figsize=(8, 4), dpi=100)
        self.fig.patch.set_facecolor('white')
        self.ax.set_facecolor('#f8f9fa')

        self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
        self.ax.set_xlabel('Czas pomiarów', fontsize=10)  # NOWE: Zmieniona etykieta
        self.ax.grid(True, alpha=0.3)
        self.ax.tick_params(axis='both', which='major', labelsize=9)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.graph_container)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def reset_plot(self):
        """Resetuje wykres do ustawień pierwotnych - NOWA FUNKCJONALNOŚĆ"""
        # Wyczyść historie danych
        self.dose_history.clear()
        self.time_history.clear()

        # Zresetuj statystyki
        self.min_dose_var.set("Min: 0.00")
        self.max_dose_var.set("Max: 0.00")
        self.avg_dose_var.set("Średnia: 0.00")
        self.points_var.set("Punkty: 0")

        # Wyczyść i przerysuj wykres
        self.ax.clear()
        self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
        self.ax.set_xlabel('Czas pomiarów', fontsize=10)
        self.ax.grid(True, alpha=0.3)
        self.ax.set_ylim(0, 0.2)
        self.ax.set_title("Historia dawki - Ostatnie 4 godziny", fontsize=10, pad=8)

        self.canvas.draw()
        self.log_message("Wykres zresetowany do ustawień początkowych")

    def toggle_auto_update(self):
        """Włącza/wyłącza automatyczną aktualizację podglądu mapy"""
        if self.auto_update_var.get():
            self.auto_map_update = True
            self.start_auto_map_update()
            self.log_message("Włączono automatyczną aktualizację podglądu mapy")
        else:
            self.auto_map_update = False
            self.log_message("Wyłączono automatyczną aktualizację podglądu mapy")

    def start_auto_map_update(self):
        """Rozpoczyna automatyczną aktualizację podglądu mapy"""
        if self.auto_map_update:
            self.update_realtime_map_preview()
            self.map_update_job = self.root.after(15000, self.start_auto_map_update)

    def update_realtime_map_preview(self):
        """Aktualizuje podgląd mapy w czasie rzeczywistym"""
        valid_points = [d for d in self.historical_data
                        if d.latitude != '00.000000' and d.longitude != '00.000000']

        points_count = len(valid_points)
        dose_stats = {'dobre': 0, 'podwyższone': 0, 'zagrożenie': 0}

        for point in valid_points:
            try:
                dose = float(point.average_dose)
                if dose < 0.15:
                    dose_stats['dobre'] += 1
                elif dose < 1.0:
                    dose_stats['podwyższone'] += 1
                else:
                    dose_stats['zagrożenie'] += 1
            except:
                continue

        self.map_preview_text.config(state=tk.NORMAL)
        self.map_preview_text.delete(1.0, tk.END)

        # Aktualne informacje
        preview_info = f"""🗺️ DANE MAPY POMIARÓW PROMIENIOWANIA - CZAS RZECZYWISTY

📊 STATYSTYKI PUNKTÓW (aktualne):
• Łączna liczba punktów: {points_count}
• ZIELONY (<0.15 μSv/h): {dose_stats['dobre']} punktów
• POMARAŃCZOWY (0.15-1.0 μSv/h): {dose_stats['podwyższone']} punktów  
• CZERWONY (>1.0 μSv/h): {dose_stats['zagrożenie']} punktów

🕒 Ostatnia aktualizacja: {datetime.now().strftime('%H:%M:%S')}

📍 OSTATNIE PUNKTY POMIAROWE:
"""

        self.map_preview_text.insert(tk.END, preview_info)

        # Kolorowanie statystyk
        self.map_preview_text.tag_add("green", "5.2", "5.9")
        self.map_preview_text.tag_add("orange", "6.2", "6.13")
        self.map_preview_text.tag_add("red", "7.2", "7.9")

        # Ostatnie punkty (maksymalnie 15)
        recent_points = valid_points[-15:]

        for i, point in enumerate(recent_points[::-1]):
            try:
                dose = float(point.average_dose)
                if dose < 0.15:
                    color_tag = "green"
                    emoji = "🟢"
                elif dose < 1.0:
                    color_tag = "orange"
                    emoji = "🟠"
                else:
                    color_tag = "red"
                    emoji = "🔴"

                point_text = f"\n{emoji} {point.time} - N:{point.latitude} E:{point.longitude} - {dose:.3f} μSv/h"
                start_pos = self.map_preview_text.index(tk.END)
                self.map_preview_text.insert(tk.END, point_text)
                end_pos = self.map_preview_text.index(tk.END)

                # Kolorowanie całej linii punktu
                self.map_preview_text.tag_add(color_tag, f"{start_pos}+1c", end_pos)

            except:
                continue

        # Informacja o automatycznej aktualizacji
        if self.auto_map_update:
            auto_info = f"\n\n🔄 Automatyczna aktualizacja: WŁĄCZONA (co 15s)"
            self.map_preview_text.insert(tk.END, auto_info)
            self.map_preview_text.tag_add("blue", tk.END + "-2l", tk.END)

        self.map_preview_text.config(state=tk.DISABLED)

    def refresh_ports(self):
        """Odświeża listę portów COM"""
        ports = serial.tools.list_ports.comports()
        port_list = [f"{port.device} - {port.description}" for port in ports]
        self.port_combobox['values'] = port_list

        if port_list:
            if self.last_port:
                for port in port_list:
                    if self.last_port in port:
                        self.port_combobox.set(port)
                        break
                else:
                    self.port_combobox.set(port_list[0])
            else:
                self.port_combobox.set(port_list[0])

    def connect_serial(self):
        """Nawiązuje połączenie z portem szeregowym"""
        port_selection = self.port_combobox.get()
        port = port_selection.split(' - ')[0] if ' - ' in port_selection else port_selection

        if not port:
            messagebox.showwarning("Uwaga", "Wybierz port COM!")
            return

        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=self.BAUDRATE,
                timeout=self.SERIAL_TIMEOUT
            )

            self.last_port = port
            self.save_last_port()

            self.open_log_file()
            self.reading_event.set()

            self.read_thread = threading.Thread(target=self.read_serial_data, daemon=True)
            self.read_thread.start()

            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self.port_combobox.config(state=tk.DISABLED)
            self.status_label.config(text="Połączono", foreground="green")
            self.map_btn.config(state=tk.NORMAL)

            self.log_message(f"Połączono z {port}")

        except serial.SerialException as e:
            messagebox.showerror("Błąd", f"Nie można połączyć z {port}: {e}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nieoczekiwany błąd: {e}")

    def disconnect_serial(self):
        """Zamyka połączenie szeregowe"""
        self.reading_event.clear()

        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()

        self.close_log_file()

        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.port_combobox.config(state=tk.NORMAL)
        self.status_label.config(text="Rozłączono", foreground="red")
        self.map_btn.config(state=tk.DISABLED)

        # Wyłącz automatyczną aktualizację
        self.auto_map_update = False
        self.auto_update_var.set(False)
        if self.map_update_job:
            self.root.after_cancel(self.map_update_job)

        self.log_message("Rozłączono z portu szeregowego")

    def read_serial_data(self):
        """Wątek odczytujący dane z portu szeregowego"""
        buffer = ""

        while self.reading_event.is_set():
            try:
                if self.serial_port and self.serial_port.is_open:
                    data = self.serial_port.read(self.serial_port.in_waiting or 1).decode('utf-8')
                    buffer += data

                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()

                        if line:
                            self.data_queue.put(('data', line))

            except (serial.SerialException, UnicodeDecodeError) as e:
                self.data_queue.put(('error', f"Błąd komunikacji: {e}"))
                break
            except Exception as e:
                self.data_queue.put(('error', f"Nieoczekiwany błąd: {e}"))
                break

    def process_queue(self):
        """Przetwarza dane z kolejki"""
        try:
            while True:
                msg_type, data = self.data_queue.get_nowait()

                if msg_type == 'data':
                    self.process_serial_data(data)
                elif msg_type == 'error':
                    self.log_message(data)
                    messagebox.showerror("Błąd", data)

        except queue.Empty:
            pass

        self.root.after(100, self.process_queue)

    def process_serial_data(self, data):
        """Przetwarza dane z urządzenia"""
        self.log_message(data)
        self.write_to_log(data)

        parsed_data = self.parse_data(data)
        if parsed_data:
            self.update_display(parsed_data)
            self.update_plot(float(parsed_data.average_dose))
            self.update_stats()

            # Aktualizuj podgląd mapy w czasie rzeczywistym jeśli jest włączona automatyczna aktualizacja
            if self.auto_map_update:
                self.update_realtime_map_preview()

    def parse_data(self, data):
        """Parsuje surowe dane do struktury GeigerData"""
        try:
            parts = data.split('|')
            if len(parts) >= 10:
                geiger_data = GeigerData(
                    date=parts[0],
                    time=parts[1],
                    latitude=parts[2],
                    longitude=parts[3],
                    altitude=parts[4],
                    satellites=parts[5],
                    hdop=parts[6],
                    accuracy=parts[7],
                    current_dose=parts[8],
                    average_dose=parts[9]
                )

                self.historical_data.append(geiger_data)
                if len(self.historical_data) > 1000:
                    self.historical_data.pop(0)

                return geiger_data
        except Exception as e:
            self.log_message(f"Błąd parsowania: {e}")

        return None

    def update_display(self, data):
        """Aktualizuje interfejs użytkownika"""
        self.current_data = data

        self.current_dose_var.set(f"{data.current_dose} μSv")
        self.average_dose_var.set(f"{data.average_dose} μSv/h")
        self.lat_var.set(f"N: {data.latitude}")
        self.lon_var.set(f"E: {data.longitude}")
        self.date_var.set(f"Data: {data.date}")
        self.time_var.set(f"Czas: {data.time}")
        self.alt_var.set(f"Wysokość: {data.altitude} m")
        self.sat_var.set(f"Satelity: {data.satellites}")
        self.hdop_var.set(f"HDOP: {data.hdop}")

    def update_plot(self, dose_value):
        """Aktualizuje wykres SŁUPKOWY - OŚ X CZASOWA Z GPS"""
        # Pobierz aktualny czas z danych GPS lub systemowy
        current_time = datetime.now()

        # Spróbuj pobrać czas z aktualnych danych GPS
        if hasattr(self, 'current_data') and self.current_data.time != "00:00:00":
            try:
                time_str = f"{self.current_data.date.split('.')[2][:4]}-{self.current_data.date.split('.')[1]}-{self.current_data.date.split('.')[0]} {self.current_data.time}"
                current_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except:
                current_time = datetime.now()

        self.dose_history.append(dose_value)
        self.time_history.append(current_time)

        # Utrzymujemy tylko ostatnie 4 godziny danych
        if len(self.dose_history) > self.MAX_DATA_POINTS:
            self.dose_history.pop(0)
            self.time_history.pop(0)

        self.ax.clear()

        if len(self.dose_history) > 0:
            # Używamy czasu jako osi X - KONWERSJA NA MATPLOTLIB DATES
            times_float = [mdates.date2num(t) for t in self.time_history]

            # Oblicz optymalną szerokość słupka na podstawie odstępu czasowego
            if len(times_float) > 1:
                time_diff = times_float[-1] - times_float[0]
                bar_width = (time_diff / len(times_float)) * 0.8  # 80% odstępu
            else:
                bar_width = 0.0007  # Domyślna szerokość (~1 minuta)

            # Rysuj słupki z czasem na osi X
            bars = self.ax.bar(times_float, self.dose_history,
                               width=bar_width,
                               color='red', alpha=0.7, edgecolor='darkred',
                               align='center')

            # Podświetl najnowszy słupek
            if bars:
                bars[-1].set_color('darkred')
                bars[-1].set_alpha(1.0)

        self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
        self.ax.set_xlabel('Czas pomiarów [UTC]', fontsize=10)

        # KONFIGURACJA OSI X - ZMNIEJSZONA LICZBA ETYKIET
        if len(self.time_history) > 0:
            # Oblicz zakres czasowy w godzinach
            if len(self.time_history) > 1:
                time_range = self.time_history[-1] - self.time_history[0]
                hours_range = time_range.total_seconds() / 3600
            else:
                hours_range = 4  # domyślnie 4 godziny

            # MNIEJ ETYKIET - bardziej agresywne grupowanie
            if hours_range <= 2:  # Do 2 godzin
                locator = mdates.MinuteLocator(interval=30)  # Co 30 minut
                formatter = mdates.DateFormatter('%H:%M')
            elif hours_range <= 6:  # Do 6 godzin
                locator = mdates.HourLocator(interval=1)  # Co godzinę
                formatter = mdates.DateFormatter('%H:%M')
            else:  # Powyżej 6 godzin
                locator = mdates.HourLocator(interval=2)  # Co 2 godziny
                formatter = mdates.DateFormatter('%H:%M')

            self.ax.xaxis.set_major_locator(locator)
            self.ax.xaxis.set_major_formatter(formatter)

            # USUŃ MNIEJSZE ETYKIETY
            self.ax.xaxis.set_minor_locator(mdates.HourLocator(interval=1))

            # Obróć etykiety i ustaw odstępy
            plt.setp(self.ax.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)

            # Automatyczne dostosowanie layoutu
            self.ax.tick_params(axis='x', which='major', pad=5)

        self.ax.grid(True, alpha=0.3, axis='y')

        # Skala osi Y
        if self.dose_history:
            y_max = max(self.dose_history)
            y_min = 0

            if y_max < 0.15:
                y_max = 0.15

            margin = y_max * 0.1
            self.ax.set_ylim(y_min, y_max + margin)

            # Automatyczne dostosowanie skali osi X do danych czasowych
            if len(self.time_history) > 1:
                padding = (self.time_history[-1] - self.time_history[0]) * 0.05
                self.ax.set_xlim(self.time_history[0] - padding,
                                 self.time_history[-1] + padding)
        else:
            self.ax.set_ylim(0, 0.2)

        # Tytuł wykresu
        if len(self.time_history) > 1:
            start_time = self.time_history[0].strftime('%H:%M')
            end_time = self.time_history[-1].strftime('%H:%M')
            total_points = len(self.dose_history)
            time_info = f"Zakres: {start_time} - {end_time} UTC | Próbki: {total_points}"
            self.ax.set_title(time_info, fontsize=9, pad=8)

        # ZWIĘKSZ MARGINESY dla lepszej czytelności
        self.fig.subplots_adjust(bottom=0.15, left=0.1, right=0.95, top=0.9)
        self.canvas.draw()

    def update_stats(self):
        """Aktualizuje statystyki"""
        if self.dose_history:
            min_dose = min(self.dose_history)
            max_dose = max(self.dose_history)
            avg_dose = sum(self.dose_history) / len(self.dose_history)

            self.min_dose_var.set(f"Min: {min_dose:.2f}")
            self.max_dose_var.set(f"Max: {max_dose:.2f}")
            self.avg_dose_var.set(f"Średnia: {avg_dose:.2f}")
            self.points_var.set(f"Punkty: {len(self.dose_history)}")

    def generate_map(self):
        """Funkcja dla przycisku w szybkich akcjach"""
        return self.generate_and_show_map()

    def generate_and_show_map(self):
        """Generuje mapę i pokazuje informacje w podglądzie"""
        if not FOLIUM_AVAILABLE:
            messagebox.showwarning("Uwaga", "Folium nie jest zainstalowane. Zainstaluj: pip install folium")
            return

        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do wygenerowania mapy")
            return

        try:
            self.map_status_var.set("Generowanie mapy...")
            self.root.update()

            # FILTRUJ TYLKO PRAWDŁOWE PUNKTY GPS
            valid_points = []
            for data in self.historical_data:
                try:
                    lat = float(data.latitude)
                    lon = float(data.longitude)
                    # Sprawdź czy współrzędne są realistyczne (Polska)
                    if 49.0 <= lat <= 55.0 and 14.0 <= lon <= 24.0:
                        valid_points.append(data)
                except (ValueError, TypeError):
                    continue

            print(
                f"DEBUG: Znaleziono {len(valid_points)} prawidłowych punktów z {len(self.historical_data)} wszystkich")

            if not valid_points:
                messagebox.showinfo("Info", "Brak prawidłowych danych GPS dla mapy")
                self.map_status_var.set("Brak danych GPS")
                return

            # ŚRODEK MAPY - uśrednij wszystkie punkty
            lats = []
            lons = []
            for data in valid_points:
                try:
                    lat = float(data.latitude)
                    lon = float(data.longitude)
                    lats.append(lat)
                    lons.append(lon)
                except ValueError:
                    continue

            center_lat = sum(lats) / len(lats)
            center_lon = sum(lons) / len(lons)

            m = folium.Map(
                location=[center_lat, center_lon],
                zoom_start=15,
                tiles='OpenStreetMap'
            )

            points_added = 0
            dose_stats = {'dobre': 0, 'podwyższone': 0, 'zagrożenie': 0}

            # LISTA PUNKTÓW DLA LINII
            line_points = []

            for data in valid_points:
                try:
                    lat = float(data.latitude)
                    lon = float(data.longitude)
                    dose = float(data.average_dose)

                    # Dodaj punkt do linii
                    line_points.append([lat, lon])

                    # NOWE ZAKRESY KOLORÓW
                    if dose < 0.15:
                        color = 'green'
                        dose_stats['dobre'] += 1
                    elif dose < 1.0:
                        color = 'orange'
                        dose_stats['podwyższone'] += 1
                    else:
                        color = 'red'
                        dose_stats['zagrożenie'] += 1

                    popup_text = f"""
                    <div style="font-family: Arial; font-size: 12px;">
                        <h4>Pomiar Promieniowania</h4>
                        <b>Dawka: {dose:.3f} μSv/h</b><br>
                        Data: {data.date}<br>
                        Czas: {data.time}<br>
                        Wysokość: {data.altitude} m<br>
                        Satelity: {data.satellites}<br>
                        HDOP: {data.hdop}
                    </div>
                    """

                    # DODAJ PUNKT NA MAPE
                    folium.CircleMarker(
                        location=[lat, lon],
                        radius=6,
                        popup=folium.Popup(popup_text, max_width=300),
                        tooltip=f"{data.time} - {dose:.3f} μSv/h",
                        color=color,
                        fillColor=color,
                        fillOpacity=0.8,
                        weight=2
                    ).add_to(m)

                    points_added += 1

                except (ValueError, TypeError) as e:
                    print(f"DEBUG: Błąd punktu {data}: {e}")
                    continue

            # DODAJ LINIĘ ŁĄCZĄCĄ PUNKTY (jeśli są co najmniej 2)
            if len(line_points) >= 2:
                folium.PolyLine(
                    locations=line_points,
                    color='blue',
                    weight=3,
                    opacity=0.6,
                    tooltip="Trasa pomiarów"
                ).add_to(m)

            print(f"DEBUG: Dodano {points_added} punktów na mapę")

            if points_added == 0:
                messagebox.showinfo("Info", "Nie udało się dodać żadnych punktów do mapy")
                self.map_status_var.set("Błąd punktów")
                return

            # LEGENDA - ZAKTUALIZOWANA Z NOWYMI KOLORAMI
            legend_html = '''
            <div style="position: fixed; 
                        bottom: 50px; left: 50px; width: 260px; height: 160px; 
                        background-color: white; border:2px solid grey; z-index:9999; 
                        font-size:14px; padding: 10px; border-radius: 5px;">
            <p><strong>Legenda:</strong></p>
            <p><span style="color: green;">●</span> ZIELONY < 0.15 μSv/h</p>
            <p><span style="color: orange;">●</span> POMARAŃCZOWY 0.15-1.0 μSv/h</p>
            <p><span style="color: red;">●</span> CZERWONY > 1.0 μSv/h</p>
            <p><span style="color: blue;">━━━</span> Trasa pomiarów</p>
            </div>
            '''
            m.get_root().html.add_child(folium.Element(legend_html))

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            map_filename = os.path.join(self.MAP_DIR, f"geiger_map_{timestamp}.html")
            m.save(map_filename)
            self.current_map_path = map_filename

            self.update_realtime_map_preview()

            self.map_status_var.set(f"Mapa gotowa ({points_added} punktów)")
            self.log_message(f"Wygenerowano mapę z {points_added} punktami: {map_filename}")

            webbrowser.open(f'file://{os.path.abspath(map_filename)}')
            messagebox.showinfo("Sukces",
                                f"Mapa wygenerowana pomyślnie!\n{points_added} punktów pomiarowych\nDodano linię trasy")

        except Exception as e:
            self.map_status_var.set("Błąd generowania mapy")
            self.log_message(f"Błąd generowania mapy: {e}")
            messagebox.showerror("Błąd", f"Nie udało się wygenerować mapy: {e}")

    def refresh_map_preview(self):
        """Odświeża podgląd mapy"""
        self.update_realtime_map_preview()
        self.map_status_var.set("Podgląd odświeżony")

    def open_map_in_browser(self):
        """Otwiera ostatnią wygenerowaną mapę w przeglądarce"""
        if self.current_map_path and os.path.exists(self.current_map_path):
            webbrowser.open(f'file://{os.path.abspath(self.current_map_path)}')
            self.log_message(f"Otwarto mapę w przeglądarce: {self.current_map_path}")
        else:
            messagebox.showinfo("Info", "Najpierw wygeneruj mapę")

    def export_kml(self):
        """Eksportuje dane do formatu KML"""
        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do eksportu")
            return

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            kml_filename = os.path.join(self.LOG_DIR, f"geiger_data_{timestamp}.kml")

            # Tworzenie głównego elementu KML
            kml = ET.Element('kml', xmlns='http://www.opengis.net/kml/2.2')
            document = ET.SubElement(kml, 'Document')

            # Nazwa dokumentu
            name = ET.SubElement(document, 'name')
            name.text = f"Pomiary Geigera - {timestamp}"

            # Style dla różnych poziomów promieniowania
            styles = {
                'green': ET.SubElement(document, 'Style', id='green_style'),
                'orange': ET.SubElement(document, 'Style', id='orange_style'),
                'red': ET.SubElement(document, 'Style', id='red_style')
            }

            for color, style_elem in styles.items():
                icon_style = ET.SubElement(style_elem, 'IconStyle')
                color_elem = ET.SubElement(icon_style, 'color')
                if color == 'green':
                    color_elem.text = 'ff00ff00'
                elif color == 'orange':
                    color_elem.text = 'ff0080ff'
                else:
                    color_elem.text = 'ff0000ff'

                scale = ET.SubElement(icon_style, 'scale')
                scale.text = '1.2'

            # Dodawanie punktów pomiarowych
            valid_points = [d for d in self.historical_data
                            if d.latitude != '00.000000' and d.longitude != '00.000000']

            for data in valid_points:
                try:
                    lat = float(data.latitude)
                    lon = float(data.longitude)
                    dose = float(data.average_dose)

                    if dose < 0.15:
                        style_url = '#green_style'
                    elif dose < 1.0:
                        style_url = '#orange_style'
                    else:
                        style_url = '#red_style'

                    placemark = ET.SubElement(document, 'Placemark')

                    name_elem = ET.SubElement(placemark, 'name')
                    name_elem.text = f"{dose:.3f} μSv/h"

                    description = ET.SubElement(placemark, 'description')
                    description.text = f"""
                    Data: {data.date}
                    Czas: {data.time}
                    Dawka: {dose:.3f} μSv/h
                    Wysokość: {data.altitude} m
                    Satelity: {data.satellites}
                    HDOP: {data.hdop}
                    """

                    style = ET.SubElement(placemark, 'styleUrl')
                    style.text = style_url

                    point = ET.SubElement(placemark, 'Point')
                    coordinates = ET.SubElement(point, 'coordinates')
                    coordinates.text = f"{lon},{lat},0"

                except (ValueError, TypeError):
                    continue

            tree = ET.ElementTree(kml)
            tree.write(kml_filename, encoding='utf-8', xml_declaration=True)

            self.log_message(f"Dane wyeksportowane do KML: {kml_filename}")
            messagebox.showinfo("Sukces", f"Dane wyeksportowane do: {kml_filename}")

        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się wyeksportować danych KML: {e}")

    def log_message(self, message):
        """Dodaje wiadomość do obszaru logów"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}\n"

        self.log_text.insert(tk.END, log_entry)
        self.log_text.see(tk.END)

        lines = self.log_text.get(1.0, tk.END).split('\n')
        if len(lines) > 500:
            self.log_text.delete(1.0, f"{len(lines) - 500}.0")

    def clear_logs(self):
        """Czyści obszar logów"""
        self.log_text.delete(1.0, tk.END)

    def save_logs(self):
        """Zapisuje logi do pliku"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_filename = os.path.join(self.LOG_DIR, f"app_log_{timestamp}.txt")

            with open(log_filename, 'w', encoding='utf-8') as f:
                f.write(self.log_text.get(1.0, tk.END))

            self.log_message(f"Logi zapisane: {log_filename}")
            messagebox.showinfo("Sukces", f"Logi zapisane do: {log_filename}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się zapisać logów: {e}")

    def open_log_folder(self):
        """Otwiera folder z logami"""
        os.startfile(self.LOG_DIR)

    def export_data(self):
        """Eksportuje dane do pliku CSV"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_filename = os.path.join(self.LOG_DIR, f"geiger_data_{timestamp}.csv")

            with open(csv_filename, 'w', encoding='utf-8') as f:
                f.write("Data;Czas;Szerokość;Długość;Wysokość;Satelity;HDOP;Dawka_chwilowa;Dawka_uśredniona\n")
                for data in self.historical_data:
                    f.write(
                        f"{data.date};{data.time};{data.latitude};{data.longitude};{data.altitude};{data.satellites};{data.hdop};{data.current_dose};{data.average_dose}\n")

            self.log_message(f"Dane wyeksportowane: {csv_filename}")
            messagebox.showinfo("Sukces", f"Dane wyeksportowane do: {csv_filename}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się wyeksportować danych: {e}")

    def open_log_file(self):
        """Otwiera nowy plik logu"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(self.LOG_DIR, f"geiger_log_{timestamp}.mx")

        try:
            self.log_file = open(self.log_filename, 'w', encoding='utf-8')
            self.log_message(f"Otwarto plik logu: {self.log_filename}")
        except IOError as e:
            self.log_message(f"Błąd otwarcia pliku logu: {e}")

    def write_to_log(self, data):
        """Zapisuje dane do pliku logu"""
        if self.log_file:
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.log_file.write(f"{timestamp}|{data}\n")
                self.log_file.flush()
            except IOError as e:
                self.log_message(f"Błąd zapisu do logu: {e}")

    def close_log_file(self):
        """Zamyka plik logu"""
        if self.log_file:
            try:
                self.log_file.close()
                self.log_message("Zamknięto plik logu")
            except IOError as e:
                self.log_message(f"Błąd zamykania pliku logu: {e}")

    def on_closing(self):
        """Zarządza zamknięciem aplikacji"""
        if self.map_update_job:
            self.root.after_cancel(self.map_update_job)

        self.disconnect_serial()
        self.root.destroy()


def main():
    """Główna funkcja aplikacji"""
    root = tk.Tk()
    app = ModernSerialReaderApp(root)

    root.protocol("WM_DELETE_WINDOW", app.on_closing)

    root.mainloop()


if __name__ == "__main__":
    main()