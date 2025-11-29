#!/usr/bin/env python3
# geiger_v25_improved.py
"""
Ulepszona wersja aplikacji Geigera z lepszym wykresem i 4-poziomową skalą kolorów.
"""

import os
import sys
import json
import threading
import queue
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from PIL import Image, ImageTk

# biblioteki opcjonalne
try:
    import serial
    import serial.tools.list_ports

    SERIAL_AVAILABLE = True
except Exception:
    SERIAL_AVAILABLE = False

try:
    import folium
    from folium import Popup

    FOLIUM_AVAILABLE = True
except Exception:
    FOLIUM_AVAILABLE = False

import matplotlib

matplotlib.use("TkAgg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


# ---------- pomocnicze funkcje ----------
def resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS  # type: ignore
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def ensure_dir(path: str):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


# ---------- dane ----------
@dataclass
class GeigerData:
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
    timestamp: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


# ---------- aplikacja ----------
class ModernSerialReaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root

        # Konfiguracja
        self.APP_TITLE = "Wer. 2.6 DRONE GPS GEIGER - 15LBOT"
        self.WINDOW_SIZE = "1200x800"
        self.MIN_WINDOW_SIZE = "1000x600"

        # komunikacja
        self.BAUDRATE = 1200
        self.SERIAL_TIMEOUT = 0.1

        # historia i limity
        self.HISTORY_HOURS = 4
        self.UPDATE_INTERVAL = 15  # (s) used for map auto-update
        self.PLOT_UPDATE_MIN_INTERVAL = 3.0  # rate-limit wykresu (s)
        self.MAX_DATA_POINTS = (self.HISTORY_HOURS * 3600) // max(1, self.UPDATE_INTERVAL)

        # NOWE: Poziomy dawki według norm
        self.DOSE_LEVELS = {
            'normal': (0.0, 0.10, '🟢', 'green'),  # Tło naturalne
            'elevated': (0.10, 0.25, '🟡', 'yellow'),  # Podwyższone
            'warning': (0.25, 1.0, '🟠', 'orange'),  # Ostrzeżenie
            'danger': (1.0, float('inf'), '🔴', 'red')  # Niebezpieczne
        }

        # NOWE: Filtrowanie danych
        self.short_term_window = 16  # 16 ostatnich próbek do uśredniania
        self.moving_avg_window = 5  # uśrednianie chwilowych wartości

        # ścieżki
        self.LOG_DIR = os.path.abspath("C:/logi_geiger/") if sys.platform.startswith("win") else os.path.abspath(
            "./logi_geiger/")
        self.MAP_DIR = os.path.join(self.LOG_DIR, "maps")
        self.RESOURCE_DIR = resource_path("resources")
        self.CONFIG_FILE = os.path.join(self.LOG_DIR, "app_config.json")

        ensure_dir(self.LOG_DIR)
        ensure_dir(self.MAP_DIR)

        # styl/UI colors
        self.COLORS = {
            'bg_light': '#f0f0f0',
            'bg_dark': '#2d2d30',
            'accent': '#007acc',
            'success': '#107c10',
            'warning': '#d83b01',
            'danger': '#e81123',
            'text': '#323130'
        }

        # zmienne runtime
        self.serial_port = None
        self.read_thread: Optional[threading.Thread] = None
        self.reading_event = threading.Event()
        self.data_queue = queue.Queue()
        self.log_file = None
        self.log_filename = None

        self.current_data = GeigerData()
        self.historical_data: List[GeigerData] = []

        # POPRAWIONE: Listy danych do wykresu - DODANA OSOBNA LISTA DLA WARTOŚCI CHWILOWYCH
        self.raw_dose_history: List[float] = []  # Surowa wartość chwilowa z urządzenia
        self.filtered_dose_history: List[float] = []  # Wartości chwilowe (przefiltrowane) - DO WYKRESU
        self.short_term_history: List[float] = []  # Średnia z 16 ostatnich próbek
        self.long_term_history: List[float] = []  # Średnia globalna
        self.alarm_points: List[tuple] = []  # Punkty alarmowe (czas, wartość)
        self.time_history: List[datetime] = []

        self.alarm_threshold = 1.0  # próg alarmowy [μSv/h]

        self.last_port = ""
        self.auto_map_update = False
        self.map_update_job = None
        self.current_map_path = None

        # rate-limit wykresu
        self._last_plot_update = 0.0

        # inicjalizacja UI i plotu
        self.load_last_port()
        self.setup_modern_ui()
        self.setup_plot()

        # uruchom pętlę przetwarzania kolejki (w wątku GUI - bezpieczne)
        self._process_queue_job = self.root.after(100, self.process_queue)

    # ---------- konfiguracja pliku konfiguracyjnego ----------
    def load_last_port(self):
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    self.last_port = cfg.get('last_port', '')
        except Exception as e:
            print(f"[CONFIG] Błąd ładowania konfiguracji: {e}")

    def save_last_port(self):
        try:
            cfg = {'last_port': self.last_port}
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
        except Exception as e:
            print(f"[CONFIG] Błąd zapisu konfiguracji: {e}")

    # ---------- NOWE: Funkcje filtrowania ----------
    def apply_moving_average(self, new_value: float) -> float:
        """Stosuje filtr uśredniający do wartości chwilowych"""
        self.raw_dose_history.append(new_value)

        if len(self.raw_dose_history) >= self.moving_avg_window:
            # Uśrednij z okna
            avg = sum(self.raw_dose_history[-self.moving_avg_window:]) / self.moving_avg_window
            return avg
        else:
            return new_value

    def calculate_short_term_avg(self) -> float:
        """Oblicza średnią z 16 ostatnich przefiltrowanych próbek"""
        if len(self.filtered_dose_history) == 0:
            return 0.0

        window = self.filtered_dose_history[-self.short_term_window:]
        return sum(window) / len(window)

    def calculate_long_term_avg(self) -> float:
        """Oblicza średnią globalną ze wszystkich przefiltrowanych próbek"""
        if len(self.filtered_dose_history) == 0:
            return 0.0
        return sum(self.filtered_dose_history) / len(self.filtered_dose_history)

    def get_dose_color(self, dose_value: float) -> str:
        """Zwraca kolor odpowiadający poziomowi dawki"""
        for level, (min_val, max_val, emoji, color) in self.DOSE_LEVELS.items():
            if min_val <= dose_value < max_val:
                return color
        return 'red'  # domyślnie czerwony

    # ---------- UI ----------
    def setup_modern_ui(self):
        self.root.title(self.APP_TITLE)
        self.root.geometry(self.WINDOW_SIZE)
        self.root.minsize(1000, 600)
        self.root.configure(bg=self.COLORS['bg_light'])
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except Exception:
            pass

        main_container = ttk.Frame(self.root)
        main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # kontrolki po lewej
        self.create_control_panel(main_container)

        # prawe okno z zakładkami
        self.create_content_panel(main_container)

    def create_control_panel(self, parent):
        control_frame = ttk.LabelFrame(parent, text=" Sterowanie ", padding=10)
        control_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        ttk.Label(control_frame, text="Port COM:").pack(anchor=tk.W, pady=(0, 5))
        self.port_combobox = ttk.Combobox(control_frame, width=20, state='readonly')
        self.port_combobox.pack(fill=tk.X, pady=(0, 10))

        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        self.refresh_btn = ttk.Button(btn_frame, text="Odśwież", command=self.refresh_ports)
        self.refresh_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.connect_btn = ttk.Button(btn_frame, text="Połącz", command=self.connect_serial)
        self.connect_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.disconnect_btn = ttk.Button(control_frame, text="Rozłącz", command=self.disconnect_serial,
                                         state=tk.DISABLED)
        self.disconnect_btn.pack(fill=tk.X, pady=5)

        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, pady=10)
        ttk.Label(status_frame, text="Status:").pack(anchor=tk.W)
        self.status_label = ttk.Label(status_frame, text="Niepołączono", foreground="red", font=('Segoe UI', 9, 'bold'))
        self.status_label.pack(anchor=tk.W)

        ttk.Separator(control_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        ttk.Label(control_frame, text="Szybkie akcje:", font=('Segoe UI', 9, 'bold')).pack(anchor=tk.W)
        self.map_btn = ttk.Button(control_frame, text="Generuj mapę", command=self.generate_and_show_map,
                                  state=tk.DISABLED)
        self.map_btn.pack(fill=tk.X, pady=5)

        ttk.Button(control_frame, text="Resetuj wykres", command=self.reset_plot).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Otwórz folder logów", command=self.open_log_folder).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Eksportuj dane (CSV)", command=self.export_data).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Eksportuj dane (KML)", command=self.export_kml).pack(fill=tk.X, pady=5)

        # logo
        logo_frame = ttk.Frame(control_frame)
        logo_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        self.logo1_photo = None
        self.logo2_photo = None
        self._load_logos(logo_frame)

        self.refresh_ports()

    def _load_logos(self, parent):
        """Wczytaj i zbuforuj logo (jednorazowo). Ignoruj błędy."""
        for fname, attr in [("logo.jpg", "logo2_photo"), ("15lbot.jpg", "logo1_photo")]:
            try:
                p = resource_path(fname)
                if os.path.exists(p):
                    img = Image.open(p).convert("RGBA")
                    # usuwamy białe tło
                    datas = img.getdata()
                    new_data = []
                    for item in datas:
                        if item[0] > 240 and item[1] > 240 and item[2] > 240:
                            new_data.append((255, 255, 255, 0))
                        else:
                            new_data.append(item)
                    img.putdata(new_data)
                    img = img.resize((120, 120), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    setattr(self, attr, photo)
                    lbl = tk.Label(parent, image=photo, bg=self.COLORS['bg_light'])
                    lbl.pack(pady=(0, 5))
            except Exception as e:
                print(f"[LOGO] Błąd ładowania {fname}: {e}")

    def create_content_panel(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.create_monitoring_tab()
        self.create_map_tab()
        self.create_logs_tab()

    def create_monitoring_tab(self):
        monitor_tab = ttk.Frame(self.notebook)
        self.notebook.add(monitor_tab, text="Monitorowanie")

        data_frame = ttk.LabelFrame(monitor_tab, text=" Dane pomiarowe ", padding=10)
        data_frame.pack(fill=tk.X, pady=(0, 10))
        self.create_data_grid(data_frame)

        graph_frame = ttk.LabelFrame(monitor_tab, text=" Historia dawki - Ostatnie 4 godziny ", padding=10)
        graph_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.graph_container = ttk.Frame(graph_frame)
        self.graph_container.pack(fill=tk.BOTH, expand=True)

        stats_frame = ttk.LabelFrame(monitor_tab, text=" Statystyki ", padding=10)
        stats_frame.pack(fill=tk.X)
        self.create_stats_grid(stats_frame)

    def create_data_grid(self, parent):
        dose_frame = ttk.Frame(parent)
        dose_frame.pack(fill=tk.X, pady=5)

        # POPRAWIONE: Trzy rodzaje wartości z lepszymi nazwami
        self.current_dose_var = tk.StringVar(value="0.00 μSv")
        self.short_term_dose_var = tk.StringVar(value="0.00 μSv/h")  # ZMIANA: Średnia chwilowa
        self.long_term_dose_var = tk.StringVar(value="0.00 μSv/h")
        self.short_term_dose_r_var = tk.StringVar(value="(0.00 mR/h)")

        # Dawka chwilowa (przefiltrowana)
        ttk.Label(dose_frame, text="Dawka chwilowa:", font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(dose_frame, textvariable=self.current_dose_var, font=('Segoe UI', 12)).pack(side=tk.LEFT,
                                                                                              padx=(0, 30))

        # POPRAWIONE: Średnia chwilowa (16 próbek) - ZMIANA NAZWY
        ttk.Label(dose_frame, text="Średnia chwilowa:", font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=(0, 10))
        self.short_term_dose_label = ttk.Label(dose_frame, textvariable=self.short_term_dose_var,
                                               font=('Segoe UI', 24))
        self.short_term_dose_label.pack(side=tk.LEFT, padx=(0, 10))

        # Milirentgeny - tylko wartość w nawiasie
        self.short_term_dose_r_label = ttk.Label(dose_frame, textvariable=self.short_term_dose_r_var,
                                                 font=('Segoe UI', 14))
        self.short_term_dose_r_label.pack(side=tk.LEFT)

        # Średnia globalna - PRZENIESIONA NA DÓŁ do statystyk
        # (usunięto wyświetlanie średniej globalnej na górze)

        gps_frame = ttk.Frame(parent)
        gps_frame.pack(fill=tk.X, pady=5)
        gps_frame.columnconfigure(0, weight=1)
        gps_frame.columnconfigure(1, weight=1)
        gps_frame.columnconfigure(2, weight=1)
        gps_frame.columnconfigure(3, weight=1)

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
        self.date_var = tk.StringVar(value="Data: 00.00.00r")
        self.time_var = tk.StringVar(value="Czas Zulu: 00:00:00")
        ttk.Label(time_frame, textvariable=self.date_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(time_frame, textvariable=self.time_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

        # Dane GPS
        quality_frame = ttk.LabelFrame(gps_frame, text=" Dane GPS ", padding=5)
        quality_frame.grid(row=0, column=2, padx=5, sticky="ew")
        self.sat_var = tk.StringVar(value="Satelity: 0")
        self.hdop_var = tk.StringVar(value="HDOP: 0.0")
        self.alt_var = tk.StringVar(value="Wysokość: 0 m")
        self.acc_var = tk.StringVar(value="Dokładność: 0 m")
        ttk.Label(quality_frame, textvariable=self.sat_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(quality_frame, textvariable=self.hdop_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(quality_frame, textvariable=self.alt_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(quality_frame, textvariable=self.acc_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

        # Dawki dzienne
        daily_frame = ttk.LabelFrame(gps_frame, text=" Dawki dzienne ", padding=5)
        daily_frame.grid(row=0, column=3, padx=5, sticky="ew")
        self.hourly_dose_var = tk.StringVar(value="Godzinowa: 0.00 μSv")
        self.daily_dose_var = tk.StringVar(value="Dobowa: 0.00 μSv")
        self.hourly_r_var = tk.StringVar(value="Godzinowa: 0.00 mR")
        self.daily_r_var = tk.StringVar(value="Dobowa: 0.00 mR")
        ttk.Label(daily_frame, textvariable=self.hourly_dose_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(daily_frame, textvariable=self.daily_dose_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(daily_frame, textvariable=self.hourly_r_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(daily_frame, textvariable=self.daily_r_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

    def create_stats_grid(self, parent):
        stats_frame = ttk.Frame(parent)
        stats_frame.pack(fill=tk.X, pady=5)
        # POWIĘKSZONE: 5 kolumn zamiast 4
        for i in range(5):
            stats_frame.columnconfigure(i, weight=1)

        self.min_dose_var = tk.StringVar(value="Min: 0.00")
        self.max_dose_var = tk.StringVar(value="Max: 0.00")
        # POPRAWIONE: ZMIANA NAZWY na "Średnia globalna"
        self.avg_dose_var = tk.StringVar(value="Śr. globalna: 0.00")
        self.points_var = tk.StringVar(value="Punkty: 0")
        # NOWE: Średnia chwilowa w statystykach
        self.short_term_avg_var = tk.StringVar(value="Śr. chwilowa: 0.00")

        ttk.Label(stats_frame, textvariable=self.min_dose_var, font=('Segoe UI', 9)).grid(row=0, column=0, padx=5)
        ttk.Label(stats_frame, textvariable=self.max_dose_var, font=('Segoe UI', 9)).grid(row=0, column=1, padx=5)
        ttk.Label(stats_frame, textvariable=self.avg_dose_var,
                  font=('Segoe UI', 14),
                  foreground='blue').grid(row=0, column=2, padx=5)
        ttk.Label(stats_frame, textvariable=self.short_term_avg_var, font=('Segoe UI', 9)).grid(row=0, column=3, padx=5)
        ttk.Label(stats_frame, textvariable=self.points_var, font=('Segoe UI', 9)).grid(row=0, column=4, padx=5)

    def create_map_tab(self):
        self.map_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.map_tab, text="Mapa")

        map_control_frame = ttk.Frame(self.map_tab)
        map_control_frame.pack(fill=tk.X, pady=5)
        ttk.Button(map_control_frame, text="Generuj i pokaż mapę", command=self.generate_and_show_map).pack(
            side=tk.LEFT, padx=5)
        ttk.Button(map_control_frame, text="Otwórz w przeglądarce", command=self.open_map_in_browser).pack(side=tk.LEFT,
                                                                                                           padx=5)
        ttk.Button(map_control_frame, text="Odśwież podgląd", command=self.refresh_map_preview).pack(side=tk.LEFT,
                                                                                                     padx=5)

        self.map_status_var = tk.StringVar(value="Kliknij 'Generuj i pokaż mapę'")
        ttk.Label(map_control_frame, textvariable=self.map_status_var, font=('Segoe UI', 9)).pack(side=tk.RIGHT,
                                                                                                  padx=10)

        map_preview_frame = ttk.LabelFrame(self.map_tab, text=" Podgląd mapy w czasie rzeczywistym ", padding=10)
        map_preview_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        auto_update_frame = ttk.Frame(map_preview_frame)
        auto_update_frame.pack(fill=tk.X, pady=5)
        self.auto_update_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(auto_update_frame, text="Automatyczna aktualizacja podglądu (co 15s)",
                        variable=self.auto_update_var, command=self.toggle_auto_update).pack(side=tk.LEFT)

        self.map_preview_text = tk.Text(map_preview_frame, wrap=tk.WORD, width=80, height=20, font=('Consolas', 9),
                                        bg='white')
        scrollbar = ttk.Scrollbar(map_preview_frame, orient=tk.VERTICAL, command=self.map_preview_text.yview)
        self.map_preview_text.configure(yscrollcommand=scrollbar.set)
        self.map_preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # tagi kolorów - NOWE 4 kolory
        self.map_preview_text.tag_configure("green", foreground="green")
        self.map_preview_text.tag_configure("yellow", foreground="yellow")
        self.map_preview_text.tag_configure("orange", foreground="orange")
        self.map_preview_text.tag_configure("red", foreground="red")
        self.map_preview_text.tag_configure("blue", foreground="blue")
        self.map_preview_text.tag_configure("bold", font=('Consolas', 9, 'bold'))

        initial_info = (
            "🗺️ DANE MAPY POMIARÓW PROMIENIOWANIA - CZAS RZECZYWISTY\n\n"
            "Aby zobaczyć mapę:\n"
            "1. Połącz z urządzeniem i zbierz dane GPS\n"
            "2. Kliknij 'Generuj i pokaż mapę'\n"
            "3. Mapa zostanie wygenerowana i otwarta w przeglądarce\n\n"
            "Kolory punktów na mapie:\n"
            "• ZIELONY - dawka < 0.10 μSv/h (tło naturalne)\n"
            "• ŻÓŁTY - dawka 0.10-0.25 μSv/h (podwyższone)\n"
            "• POMARAŃCZOWY - dawka 0.25-1.0 μSv/h (ostrzeżenie)\n"
            "• CZERWONY - dawka > 1.0 μSv/h (niebezpieczne)\n"
        )
        self.map_preview_text.insert(tk.END, initial_info)
        self.map_preview_text.config(state=tk.DISABLED)

    def create_logs_tab(self):
        logs_tab = ttk.Frame(self.notebook)
        self.notebook.add(logs_tab, text="Logi")

        log_control_frame = ttk.Frame(logs_tab)
        log_control_frame.pack(fill=tk.X, pady=5)
        ttk.Button(log_control_frame, text="Wyczyść logi", command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_control_frame, text="Zapisz logi", command=self.save_logs).pack(side=tk.LEFT, padx=5)

        self.log_text = scrolledtext.ScrolledText(logs_tab, wrap=tk.WORD, width=80, height=20, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ---------- matplotlib wykres ----------
    def setup_plot(self):
        self.fig, self.ax = plt.subplots(figsize=(8, 4), dpi=100)
        self.fig.patch.set_facecolor('white')
        self.ax.set_facecolor('#f8f9fa')
        self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
        self.ax.set_xlabel('Czas pomiarów', fontsize=10)
        self.ax.grid(True, alpha=0.3)
        self.ax.tick_params(axis='both', which='major', labelsize=9)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.graph_container)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def reset_plot(self):
        self.raw_dose_history.clear()
        self.filtered_dose_history.clear()
        self.short_term_history.clear()
        self.long_term_history.clear()
        self.alarm_points.clear()
        self.time_history.clear()
        self._last_plot_update = 0.0
        self.min_dose_var.set("Min: 0.00")
        self.max_dose_var.set("Max: 0.00")
        self.avg_dose_var.set("Śr. globalna: 0.00")
        self.short_term_avg_var.set("Śr. chwilowa: 0.00")
        self.points_var.set("Punkty: 0")
        self.ax.clear()
        self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
        self.ax.set_xlabel('Czas pomiarów', fontsize=10)
        self.ax.grid(True, alpha=0.3)
        self.ax.set_ylim(0, 0.2)
        self.ax.set_title("Historia dawki - Ostatnie 4 godziny", fontsize=10, pad=8)
        self.canvas.draw()
        self.log_message("Wykres zresetowany")

    # ---------- porty szeregowe ----------
    def refresh_ports(self):
        values = []
        try:
            if SERIAL_AVAILABLE:
                ports = serial.tools.list_ports.comports()
                values = [f"{p.device} - {p.description}" for p in ports]
            else:
                values = []
        except Exception as e:
            self.log_message(f"Błąd listowania portów: {e}")
            values = []

        self.port_combobox['values'] = values
        if values:
            if self.last_port:
                for v in values:
                    if self.last_port in v:
                        self.port_combobox.set(v)
                        break
                else:
                    self.port_combobox.set(values[0])
            else:
                self.port_combobox.set(values[0])

    def connect_serial(self):
        if not SERIAL_AVAILABLE:
            messagebox.showerror("Błąd", "Biblioteka 'pyserial' nie jest dostępna.")
            return

        port_selection = self.port_combobox.get()
        port = port_selection.split(' - ')[0] if ' - ' in port_selection else port_selection
        if not port:
            messagebox.showwarning("Uwaga", "Wybierz port COM!")
            return

        try:
            self.serial_port = serial.Serial(port=port, baudrate=self.BAUDRATE, timeout=self.SERIAL_TIMEOUT)
            self.last_port = port
            self.save_last_port()
            self.open_log_file()
            self.reading_event.set()
            self.read_thread = threading.Thread(target=self._serial_read_loop, daemon=True)
            self.read_thread.start()

            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self.port_combobox.config(state=tk.DISABLED)
            self.status_label.config(text="Połączono", foreground="green")
            self.map_btn.config(state=tk.NORMAL)

            self.log_message(f"Połączono z {port}")

        except Exception as e:
            messagebox.showerror("Błąd", f"Nie można połączyć: {e}")
            self.log_message(f"Błąd łączenia: {e}")

    def disconnect_serial(self):
        # wyłącz czytanie i zamknij port
        try:
            self.reading_event.clear()
            time.sleep(0.05)  # krótka pauza żeby wątek mógł zakończyć pętlę
            if self.serial_port and getattr(self.serial_port, "is_open", False):
                try:
                    self.serial_port.close()
                except Exception:
                    pass
            self.close_log_file()

            self.connect_btn.config(state=tk.NORMAL)
            self.disconnect_btn.config(state=tk.DISABLED)
            self.port_combobox.config(state=tk.NORMAL)
            self.status_label.config(text="Rozłączono", foreground="red")
            self.map_btn.config(state=tk.DISABLED)

            # anuluj auto-update map
            self.auto_map_update = False
            self.auto_update_var.set(False)
            if self.map_update_job:
                try:
                    self.root.after_cancel(self.map_update_job)
                except Exception:
                    pass
                self.map_update_job = None

            self.log_message("Rozłączono z portu szeregowego")
        except Exception as e:
            self.log_message(f"Błąd przy rozłączaniu: {e}")

    def _serial_read_loop(self):
        """Wątek czytający z portu i wstawiający linie do kolejki"""
        buffer = ""
        while self.reading_event.is_set():
            try:
                if self.serial_port and getattr(self.serial_port, "is_open", False):
                    n = self.serial_port.in_waiting or 1
                    data = self.serial_port.read(n)
                    try:
                        text = data.decode('utf-8', errors='replace')
                    except Exception:
                        text = str(data)
                    buffer += text
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            self.data_queue.put(('data', line))
                else:
                    time.sleep(0.05)
            except Exception as e:
                self.data_queue.put(('error', f"Błąd komunikacji: {e}"))
                break

    # ---------- kolejka przetwarzania (wywoływane w GUI thread) ----------
    def process_queue(self):
        try:
            while True:
                msg_type, payload = self.data_queue.get_nowait()
                if msg_type == 'data':
                    self.process_serial_data(payload)
                elif msg_type == 'error':
                    self.log_message(payload)
                    messagebox.showerror("Błąd", payload)
        except queue.Empty:
            pass
        # ponowne wywołanie
        self._process_queue_job = self.root.after(100, self.process_queue)

    def process_serial_data(self, line: str):
        """Obsługuje odebrane linie - loguje, parsuje, aktualizuje UI i wykres"""
        self.log_message(line)
        self.write_to_log(line)

        g = self.parse_data(line)
        if not g:
            return

        # aktualizuj widok
        self.update_display(g)

        # dodaj do wykresu i statystyk; rate-limit redraw
        try:
            current_dose = safe_float(g.current_dose, 0.0)
            filtered_dose = self.apply_moving_average(current_dose)  # NOWE: filtrowanie
            self._append_history_point(g, filtered_dose)
            now = time.time()
            if now - self._last_plot_update >= self.PLOT_UPDATE_MIN_INTERVAL:
                # odrysuj natychmiast w wątku GUI
                self.update_plot()
                self._last_plot_update = now
            else:
                # tylko zaktualizuj statystyki (bez rysowania)
                self.update_stats()
        except Exception as e:
            self.log_message(f"Błąd aktualizacji wykresu: {e}")

        # automatyczny podgląd mapy (bez blokowania)
        if self.auto_map_update:
            # update_realtime_map_preview manipuluje widgetami — wykonaj w GUI (już jesteśmy w GUI)
            self.update_realtime_map_preview()

    # ---------- parsing ----------
    def parse_data(self, data: str) -> Optional[GeigerData]:
        """
        Oczekuje danych rozdzielonych '|' w kolejności:
        date|time|lat|lon|alt|sat|hdop|acc|current_dose|average_dose|...
        Zwraca GeigerData lub None.
        """
        try:
            parts = data.split('|')
            if len(parts) < 10:
                return None
            gd = GeigerData(
                date=parts[0].strip(),
                time=parts[1].strip(),
                latitude=parts[2].strip(),
                longitude=parts[3].strip(),
                altitude=parts[4].strip(),
                satellites=parts[5].strip() if len(parts) > 5 else "0",
                hdop=parts[6].strip() if len(parts) > 6 else "0",
                accuracy=parts[7].strip() if len(parts) > 7 else "0",
                current_dose=parts[8].strip(),
                average_dose=parts[9].strip(),
                timestamp=datetime.now()
            )
            # ogranicz historię
            self.historical_data.append(gd)
            if len(self.historical_data) > max(2000, int(self.MAX_DATA_POINTS * 1.5)):
                # trzymamy ograniczenie (nie rośnie w nieskończoność)
                del self.historical_data[0: len(self.historical_data) - max(2000, int(self.MAX_DATA_POINTS * 1.5))]
            return gd
        except Exception as e:
            self.log_message(f"Błąd parsowania: {e}")
            return None

    # ---------- aktualizacja widoku ----------
    def update_display(self, data: GeigerData):
        self.current_data = data

        # NOWE: Oblicz wszystkie trzy wartości
        current_dose = safe_float(data.current_dose, 0.0)
        filtered_dose = self.apply_moving_average(current_dose)
        short_term_avg = self.calculate_short_term_avg()
        long_term_avg = self.calculate_long_term_avg()

        # NOWE: Kolorowanie według wartości krótkoterminowej (najbardziej reprezentatywnej)
        color = self.get_dose_color(short_term_avg)
        self.short_term_dose_label.config(foreground=color)
        self.short_term_dose_r_label.config(foreground=color)

        # NOWE: Przeliczenie na milirentgeny
        dose_mr_value = short_term_avg * 0.1

        # NOWE: Przeliczenie dawek dziennych
        daily_dose_value = short_term_avg * 24  # μSv/d
        daily_mr_value = dose_mr_value * 24  # mR/d

        # Aktualizacja wszystkich wartości
        self.current_dose_var.set(f"{filtered_dose:.2f} μSv")
        self.short_term_dose_var.set(f"{short_term_avg:.2f} μSv/h")  # ZMIANA: Średnia chwilowa
        self.short_term_dose_r_var.set(f"({dose_mr_value:.2f} mR/h)")

        # Aktualizacja dawek dziennych
        self.hourly_dose_var.set(f"Godzinowa: {short_term_avg:.2f} μSv")
        self.daily_dose_var.set(f"Dobowa: {daily_dose_value:.2f} μSv")
        self.hourly_r_var.set(f"Godzinowa: {dose_mr_value:.2f} mR")
        self.daily_r_var.set(f"Dobowa: {daily_mr_value:.2f} mR")

        self.lat_var.set(f"N: {data.latitude}")
        self.lon_var.set(f"E: {data.longitude}")
        self.date_var.set(f"Data: {data.date}r")
        self.time_var.set(f"Czas Zulu: {data.time}")
        self.alt_var.set(f"Wysokość: {data.altitude} m")
        self.sat_var.set(f"Satelity: {data.satellites}")
        self.hdop_var.set(f"HDOP: {data.hdop}")
        self.acc_var.set(f"Dokładność: {data.accuracy} m")

    def _append_history_point(self, g: GeigerData, filtered_dose: float):
        """Dodaj pojedynczy punkt do historii z filtrowaniem - POPRAWIONE!"""
        # czas
        t = self._parse_gps_datetime_safe(g.date, g.time)
        self.time_history.append(t)

        # ZAPISUJEMY przefiltrowaną wartość chwilową do historii WYKRESU
        self.filtered_dose_history.append(filtered_dose)

        # oblicz wszystkie średnie
        short_term_avg = self.calculate_short_term_avg()
        long_term_avg = self.calculate_long_term_avg()

        self.short_term_history.append(short_term_avg)
        self.long_term_history.append(long_term_avg)

        # wykrywanie alarmów
        if filtered_dose > self.alarm_threshold:
            self.alarm_points.append((t, filtered_dose))

        # obcięcie - UPEWNIAMY SIĘ ŻE WSZYSTKIE LISTY SĄ TAKIEJ SAMEJ DŁUGOŚCI
        max_points = max(1, int(self.MAX_DATA_POINTS))
        if len(self.filtered_dose_history) > max_points:
            excess = len(self.filtered_dose_history) - max_points
            del self.raw_dose_history[0:excess]
            del self.filtered_dose_history[0:excess]
            del self.short_term_history[0:excess]
            del self.long_term_history[0:excess]
            del self.time_history[0:excess]

        # DODATKOWE SPRAWDZENIE: upewnij się że wszystkie listy mają tę samą długość
        min_len = min(len(self.filtered_dose_history), len(self.short_term_history),
                      len(self.long_term_history), len(self.time_history))

        if len(self.filtered_dose_history) > min_len:
            del self.filtered_dose_history[min_len:]
        if len(self.short_term_history) > min_len:
            del self.short_term_history[min_len:]
        if len(self.long_term_history) > min_len:
            del self.long_term_history[min_len:]
        if len(self.time_history) > min_len:
            del self.time_history[min_len:]

    def _parse_gps_datetime_safe(self, date_str: str, time_str: str) -> datetime:
        # obsłuż różne formaty daty: DD.MM.YY, DD.MM.YYYY, YYYY-MM-DD, itp.
        candidates = []
        if date_str and time_str:
            candidates.append(f"{date_str} {time_str}")
            # jeśli data zawiera kropki i dwie cyfry roku -> spróbuj rozszerzyć do 20xx/19xx
            try:
                parts = date_str.split('.')
                if len(parts) == 3 and len(parts[2]) == 2:
                    # DD.MM.YY -> DD.MM.20YY jeśli mniejsze niż 50 -> 20XX, inaczej 19XX
                    yy = int(parts[2])
                    year_full = 2000 + yy if yy < 70 else 1900 + yy
                    candidates.append(f"{parts[0]}.{parts[1]}.{year_full} {time_str}")
            except Exception:
                pass

        # dopuszczalne formaty
        formats = ["%d.%m.%Y %H:%M:%S", "%d.%m.%y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"]
        for candidate in candidates:
            for fmt in formats:
                try:
                    return datetime.strptime(candidate, fmt)
                except Exception:
                    continue
        # fallback - teraz
        return datetime.now()

    def update_plot(self):
        """Rysuje ulepszony wykres z CZTEREMA warstwami informacji - POPRAWIONE!"""
        try:
            self.ax.clear()
            if self.filtered_dose_history and self.time_history:
                times_num = [mdates.date2num(t) for t in self.time_history]

                # 0. SŁUPKI - wartości chwilowe (przefiltrowane) - POPRAWIONE!
                if len(self.filtered_dose_history) > 0 and len(self.filtered_dose_history) == len(times_num):
                    # Szerokość słupka zależna od odstępu:
                    if len(times_num) > 1:
                        time_diff = times_num[-1] - times_num[0]
                        width = (time_diff / len(times_num)) * 0.6  # nieco węższe
                    else:
                        width = 1 / 1440.0  # ~1 minuta

                    bars = self.ax.bar(times_num, self.filtered_dose_history, width=width,
                                       align='center', alpha=0.3, color='lightgray',
                                       edgecolor='gray', linewidth=0.5,
                                       label='Wartości chwilowe')

                # 1. LINIA DŁUGOTERMINOWA (niebieska) - średnia globalna - POPRAWIONE!
                if len(self.long_term_history) > 0 and len(self.long_term_history) == len(times_num):
                    self.ax.plot(times_num, self.long_term_history,
                                 color='blue', linewidth=2,
                                 label='Średnia globalna')

                # 2. LINIA KRÓTKOTERMINOWA (pomarańczowa) - średnia chwilowa (16 próbek) - POPRAWIONE!
                if len(self.short_term_history) > 0 and len(self.short_term_history) == len(times_num):
                    self.ax.plot(times_num, self.short_term_history,
                                 color='orange', linewidth=2, linestyle='--',
                                 label='Średnia chwilowa')

                # 3. PUNKTY ALARMOWE (czerwone) - wartości chwilowe > progu
                if self.alarm_points:
                    alarm_times, alarm_values = zip(*self.alarm_points)
                    alarm_times_num = [mdates.date2num(t) for t in alarm_times]
                    self.ax.scatter(alarm_times_num, alarm_values,
                                    color='red', s=50, zorder=5,
                                    label=f'Alarm (> {self.alarm_threshold} μSv/h)')

                # Legenda
                self.ax.legend(loc='upper right', fontsize=8)

                # konfig osi X
                if len(self.time_history) > 1:
                    time_range = (self.time_history[-1] - self.time_history[0]).total_seconds() / 3600.0
                else:
                    time_range = self.HISTORY_HOURS

                if time_range <= 2:
                    locator = mdates.MinuteLocator(interval=30)
                    formatter = mdates.DateFormatter('%H:%M')
                elif time_range <= 6:
                    locator = mdates.HourLocator(interval=1)
                    formatter = mdates.DateFormatter('%H:%M')
                else:
                    locator = mdates.HourLocator(interval=2)
                    formatter = mdates.DateFormatter('%H:%M')

                self.ax.xaxis.set_major_locator(locator)
                self.ax.xaxis.set_major_formatter(formatter)
                plt.setp(self.ax.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)

                # Y scale - uwzględniamy wszystkie wartości
                all_values = []
                if self.filtered_dose_history:
                    all_values.extend(self.filtered_dose_history)
                if self.long_term_history:
                    all_values.extend(self.long_term_history)
                if self.short_term_history:
                    all_values.extend(self.short_term_history)
                if self.alarm_points:
                    all_values.extend([point[1] for point in self.alarm_points])

                if all_values:
                    y_max = max(max(all_values), 0.15)
                else:
                    y_max = 0.15

                margin = y_max * 0.1
                self.ax.set_ylim(0, y_max + margin)

                if len(self.time_history) > 1:
                    padding = (self.time_history[-1] - self.time_history[0]) * 0.05
                    self.ax.set_xlim(self.time_history[0] - padding, self.time_history[-1] + padding)

                # tytuł z zakresem czasowym
                if len(self.time_history) > 1:
                    start = self.time_history[0].strftime('%H:%M')
                    end = self.time_history[-1].strftime('%H:%M')
                    self.ax.set_title(f"Zakres: {start} - {end} | Próbki: {len(self.filtered_dose_history)}",
                                      fontsize=9,
                                      pad=8)

            else:
                self.ax.set_ylim(0, 0.2)
                self.ax.set_title("Brak danych", fontsize=9, pad=8)

            self.ax.set_ylabel('μSv/h', fontsize=12, fontweight='bold')
            self.ax.set_xlabel('Czas pomiarów [lokalny]', fontsize=10)
            self.ax.grid(True, alpha=0.3, axis='y')
            self.fig.subplots_adjust(bottom=0.15, left=0.1, right=0.95, top=0.9)
            self.canvas.draw()
            self.update_stats()
        except Exception as e:
            self.log_message(f"Błąd rysowania wykresu: {e}")

    def update_stats(self):
        if self.filtered_dose_history:
            mn = min(self.filtered_dose_history)
            mx = max(self.filtered_dose_history)
            avg_global = sum(self.filtered_dose_history) / len(self.filtered_dose_history)

            # NOWE: Średnia chwilowa (z ostatnich 16 próbek)
            if len(self.short_term_history) > 0:
                avg_short_term = self.short_term_history[-1]
            else:
                avg_short_term = 0.0

            self.min_dose_var.set(f"Min: {mn:.2f}")
            self.max_dose_var.set(f"Max: {mx:.2f}")
            self.avg_dose_var.set(f"Śr. globalna: {avg_global:.2f}")  # ZMIANA NAZWY
            self.short_term_avg_var.set(f"Śr. chwilowa: {avg_short_term:.2f}")  # NOWE
            self.points_var.set(f"Punkty: {len(self.filtered_dose_history)}")
        else:
            self.min_dose_var.set("Min: 0.00")
            self.max_dose_var.set("Max: 0.00")
            self.avg_dose_var.set("Śr. globalna: 0.00")  # ZMIANA NAZWY
            self.short_term_avg_var.set("Śr. chwilowa: 0.00")  # NOWE
            self.points_var.set("Punkty: 0")

    # ---------- mapa ----------
    def toggle_auto_update(self):
        self.auto_map_update = bool(self.auto_update_var.get())
        if self.auto_map_update:
            self.log_message("Włączono automatyczną aktualizację podglądu mapy")
            self._schedule_map_update()
        else:
            self.log_message("Wyłączono automatyczną aktualizację podglądu mapy")
            if self.map_update_job:
                try:
                    self.root.after_cancel(self.map_update_job)
                except Exception:
                    pass
                self.map_update_job = None

    def _schedule_map_update(self):
        if self.auto_map_update:
            self.update_realtime_map_preview()
            # zaplanuj kolejne wywołanie
            self.map_update_job = self.root.after(int(self.UPDATE_INTERVAL * 1000), self._schedule_map_update)

    def update_realtime_map_preview(self):
        # filtrujemy poprawne punkty GPS (współrzędne serio)
        valid = []
        for d in self.historical_data:
            try:
                lat = float(d.latitude)
                lon = float(d.longitude)
                valid.append(d)
            except Exception:
                continue

        points_count = len(valid)
        # NOWE: 4 poziomy zamiast 3
        stats = {'normal': 0, 'elevated': 0, 'warning': 0, 'danger': 0}
        for p in valid:
            dose = safe_float(p.average_dose)
            if dose < 0.10:
                stats['normal'] += 1
            elif dose < 0.25:
                stats['elevated'] += 1
            elif dose < 1.0:
                stats['warning'] += 1
            else:
                stats['danger'] += 1

        self.map_preview_text.config(state=tk.NORMAL)
        self.map_preview_text.delete("1.0", tk.END)

        header = (
            "🗺️ DANE MAPY POMIARÓW PROMIENIOWANIA - CZAS RZECZYWISTY\n\n"
            f"📊 STATYSTYKI PUNKTÓW (aktualne):\n"
            f"• Łączna liczba punktów: {points_count}\n"
            f"• ZIELONY (<0.10 μSv/h): {stats['normal']}\n"
            f"• ŻÓŁTY (0.10-0.25 μSv/h): {stats['elevated']}\n"
            f"• POMARAŃCZOWY (0.25-1.0 μSv/h): {stats['warning']}\n"
            f"• CZERWONY (>1.0 μSv/h): {stats['danger']}\n\n"
            f"🕒 Ostatnia aktualizacja: {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"📍 OSTATNIE PUNKTY POMIAROWE:\n"
        )
        self.map_preview_text.insert(tk.END, header)

        recent = valid[-15:]
        for p in reversed(recent):
            try:
                dose = safe_float(p.average_dose)
                if dose < 0.10:
                    tag = "green"
                    emoji = "🟢"
                elif dose < 0.25:
                    tag = "yellow"
                    emoji = "🟡"
                elif dose < 1.0:
                    tag = "orange"
                    emoji = "🟠"
                else:
                    tag = "red"
                    emoji = "🔴"
                line = f"{emoji} {p.time} - N:{p.latitude} E:{p.longitude} - {dose:.3f} μSv/h\n"
                start = self.map_preview_text.index(tk.END)
                self.map_preview_text.insert(tk.END, line)
                end = self.map_preview_text.index(tk.END)
                self.map_preview_text.tag_add(tag, start, end)
            except Exception:
                continue

        if self.auto_map_update:
            info = f"\n🔄 Automatyczna aktualizacja: WŁĄCZONA (co {self.UPDATE_INTERVAL}s)\n"
            start = self.map_preview_text.index(tk.END)
            self.map_preview_text.insert(tk.END, info)
            self.map_preview_text.tag_add("blue", start, self.map_preview_text.index(tk.END))

        self.map_preview_text.config(state=tk.DISABLED)

    def generate_and_show_map(self):
        """Główny punkt dla generowania mapy - zabezpieczone i krótsze kroki"""
        if not FOLIUM_AVAILABLE:
            messagebox.showwarning("Uwaga", "Folium nie jest zainstalowane. Zainstaluj: pip install folium")
            return
        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do wygenerowania mapy")
            return

        try:
            self.map_status_var.set("Generowanie mapy...")
            self.root.update_idletasks()

            valid_points = self._collect_valid_map_points()
            if not valid_points:
                messagebox.showinfo("Info", "Brak prawidłowych danych GPS dla mapy")
                self.map_status_var.set("Brak danych GPS")
                return

            center = self._calculate_center(valid_points)
            m = folium.Map(location=center, zoom_start=15, tiles='OpenStreetMap')

            points_added, line_points = self._add_points_to_map(m, valid_points)
            if points_added == 0:
                messagebox.showinfo("Info", "Nie udało się dodać żadnych punktów do mapy")
                self.map_status_var.set("Błąd punktów")
                return

            # dodaj linię trasy
            if len(line_points) >= 2:
                folium.PolyLine(locations=line_points, color='blue', weight=3, opacity=0.6,
                                tooltip="Trasa pomiarów").add_to(m)

            # legenda - NOWE 4 kolory
            legend_html = '''
            <div style="position: fixed; 
                        bottom: 50px; left: 50px; width: 280px; height: 180px; 
                        background-color: white; border:2px solid grey; z-index:9999; 
                        font-size:14px; padding: 10px; border-radius: 5px;">
            <p><strong>Legenda:</strong></p>
            <p><span style="color: green;">●</span> ZIELONY < 0.10 μSv/h</p>
            <p><span style="color: yellow;">●</span> ŻÓŁTY 0.10-0.25 μSv/h</p>
            <p><span style="color: orange;">●</span> POMARAŃCZOWY 0.25-1.0 μSv/h</p>
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
            self.log_message(f"Wygenerowano mapę: {map_filename}")

            # otwórz w przeglądarce
            try:
                import webbrowser
                webbrowser.open(f'file://{os.path.abspath(map_filename)}')
            except Exception:
                pass

            messagebox.showinfo("Sukces", f"Mapa wygenerowana pomyślnie!\n{points_added} punktów pomiarowych")

        except Exception as e:
            self.map_status_var.set("Błąd generowania mapy")
            self.log_message(f"Błąd generowania mapy: {e}")
            messagebox.showerror("Błąd", f"Nie udało się wygenerować mapy: {e}")

    def _collect_valid_map_points(self) -> List[GeigerData]:
        valid = []
        for d in self.historical_data:
            try:
                lat = float(d.latitude)
                lon = float(d.longitude)
                valid.append(d)
            except Exception:
                continue
        return valid

    def _calculate_center(self, points: List[GeigerData]):
        lats, lons = [], []
        for p in points:
            try:
                lats.append(float(p.latitude))
                lons.append(float(p.longitude))
            except Exception:
                continue
        if not lats or not lons:
            return (0.0, 0.0)
        return (sum(lats) / len(lats), sum(lons) / len(lons))

    def _add_points_to_map(self, m: folium.Map, points: List[GeigerData]):
        points_added = 0
        line_points = []
        for d in points:
            try:
                lat = float(d.latitude)
                lon = float(d.longitude)
                dose = float(d.average_dose)
                line_points.append([lat, lon])

                # NOWE: 4 poziomy kolorów
                if dose < 0.10:
                    color = 'green'
                elif dose < 0.25:
                    color = 'yellow'
                elif dose < 1.0:
                    color = 'orange'
                else:
                    color = 'red'

                popup_text = (
                    f"<div style='font-family: Arial; font-size:12px;'>"
                    f"<b>Dawka: {dose:.3f} μSv/h</b><br>"
                    f"Data: {d.date}r<br>Czas Zulu: {d.time}<br>Wysokość: {d.altitude} m<br>Sat: {d.satellites}<br>HDOP: {d.hdop}<br>Dokładność: {d.accuracy} m"
                    f"</div>"
                )
                folium.CircleMarker(location=[lat, lon], radius=6, popup=folium.Popup(popup_text, max_width=300),
                                    tooltip=f"{d.time} - {dose:.3f} μSv/h", color=color, fillColor=color,
                                    fillOpacity=0.8, weight=2).add_to(m)
                points_added += 1
            except Exception:
                continue
        return points_added, line_points

    def refresh_map_preview(self):
        self.update_realtime_map_preview()
        self.map_status_var.set("Podgląd odświeżony")

    def open_map_in_browser(self):
        if self.current_map_path and os.path.exists(self.current_map_path):
            try:
                import webbrowser
                webbrowser.open(f'file://{os.path.abspath(self.current_map_path)}')
                self.log_message(f"Otwarto mapę: {self.current_map_path}")
            except Exception as e:
                self.log_message(f"Błąd otwierania mapy: {e}")
        else:
            messagebox.showinfo("Info", "Najpierw wygeneruj mapę")

    # ---------- eksporty ----------
    def export_data(self):
        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do eksportu")
            return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_filename = os.path.join(self.LOG_DIR, f"geiger_data_{timestamp}.csv")
            with open(csv_filename, 'w', encoding='utf-8') as f:
                f.write(
                    "Data;Czas;Szerokość;Długość;Wysokość;Satelity;HDOP;Dokładność;Dawka_chwilowa;Dawka_uśredniona\n")
                for d in self.historical_data:
                    f.write(
                        f"{d.date};{d.time};{d.latitude};{d.longitude};{d.altitude};{d.satellites};{d.hdop};{d.accuracy};{d.current_dose};{d.average_dose}\n")
            self.log_message(f"Dane wyeksportowane: {csv_filename}")
            messagebox.showinfo("Sukces", f"Dane wyeksportowane do: {csv_filename}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się wyeksportować danych: {e}")

    def export_kml(self):
        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do eksportu")
            return
        try:
            import xml.etree.ElementTree as ET
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            kml_filename = os.path.join(self.LOG_DIR, f"geiger_data_{timestamp}.kml")
            kml = ET.Element('kml', xmlns='http://www.opengis.net/kml/2.2')
            document = ET.SubElement(kml, 'Document')
            name = ET.SubElement(document, 'name')
            name.text = f"Pomiary Geigera - {timestamp}"

            # style - NOWE 4 kolory
            styles = {
                'green': 'ff00ff00',
                'yellow': 'ff00ffff',
                'orange': 'ff0080ff',
                'red': 'ff0000ff'
            }
            for key, color_code in styles.items():
                style_elem = ET.SubElement(document, 'Style', id=f"{key}_style")
                icon = ET.SubElement(style_elem, 'IconStyle')
                c = ET.SubElement(icon, 'color')
                c.text = color_code
                s = ET.SubElement(icon, 'scale')
                s.text = '1.2'

            for d in self.historical_data:
                try:
                    lat = float(d.latitude)
                    lon = float(d.longitude)
                    dose = float(d.average_dose)
                    if dose < 0.10:
                        style_url = '#green_style'
                    elif dose < 0.25:
                        style_url = '#yellow_style'
                    elif dose < 1.0:
                        style_url = '#orange_style'
                    else:
                        style_url = '#red_style'

                    placemark = ET.SubElement(document, 'Placemark')
                    n = ET.SubElement(placemark, 'name')
                    n.text = f"{dose:.3f} μSv/h"
                    desc = ET.SubElement(placemark, 'description')
                    desc.text = f"Data: {d.date}r\nCzas Zulu: {d.time}\nDawka: {dose:.3f} μSv/h\nWysokość: {d.altitude} m\nSat: {d.satellites}\nHDOP: {d.hdop}\nDokładność: {d.accuracy} m"
                    s = ET.SubElement(placemark, 'styleUrl')
                    s.text = style_url
                    point = ET.SubElement(placemark, 'Point')
                    coords = ET.SubElement(point, 'coordinates')
                    coords.text = f"{lon},{lat},0"
                except Exception:
                    continue

            tree = ET.ElementTree(kml)
            tree.write(kml_filename, encoding='utf-8', xml_declaration=True)
            self.log_message(f"Dane wyeksportowane do KML: {kml_filename}")
            messagebox.showinfo("Sukces", f"Dane wyeksportowane do: {kml_filename}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się wyeksportować KML: {e}")

    # ---------- logi ----------
    def open_log_file(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_filename = os.path.join(self.LOG_DIR, f"geiger_log_{timestamp}.mx")
            self.log_file = open(self.log_filename, 'w', encoding='utf-8')
            self.log_message(f"Otwarto plik logu: {self.log_filename}")
        except Exception as e:
            self.log_message(f"Błąd otwierania pliku logu: {e}")
            self.log_file = None

    def write_to_log(self, line: str):
        if not self.log_file:
            return
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_file.write(f"{ts}|{line}\n")
            self.log_file.flush()
        except Exception as e:
            self.log_message(f"Błąd zapisu do logu: {e}")

    def close_log_file(self):
        if self.log_file:
            try:
                self.log_file.close()
                self.log_file = None
                self.log_message("Zamknięto plik logu")
            except Exception as e:
                self.log_message(f"Błąd zamykania pliku logu: {e}")

    def log_message(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}\n"
        try:
            self.log_text.insert(tk.END, entry)
            self.log_text.see(tk.END)
            # obetnij długość logu
            lines = int(self.log_text.index('end-1c').split('.')[0])
            if lines > 1000:
                # usuwamy najstarsze linie
                self.log_text.delete("1.0", f"{lines - 800}.0")
        except Exception:
            # fallback print
            print(entry, end='')

    def clear_logs(self):
        try:
            self.log_text.delete("1.0", tk.END)
        except Exception as e:
            print(f"[LOG] Błąd czyszczenia logów: {e}")

    def save_logs(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_filename = os.path.join(self.LOG_DIR, f"app_log_{timestamp}.txt")
            with open(log_filename, 'w', encoding='utf-8') as f:
                f.write(self.log_text.get("1.0", tk.END))
            self.log_message(f"Logi zapisane: {log_filename}")
            messagebox.showinfo("Sukces", f"Logi zapisane do: {log_filename}")
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie udało się zapisać logów: {e}")

    def open_log_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.LOG_DIR)
            elif sys.platform.startswith("darwin"):
                os.system(f"open {self.LOG_DIR}")
            else:
                os.system(f"xdg-open {self.LOG_DIR}")
        except Exception as e:
            self.log_message(f"Błąd otwierania folderu: {e}")

    # ---------- zamykanie aplikacji ----------
    def on_closing(self):
        try:
            # anuluj planowane zadania
            if self._process_queue_job:
                try:
                    self.root.after_cancel(self._process_queue_job)
                except Exception:
                    pass
            if self.map_update_job:
                try:
                    self.root.after_cancel(self.map_update_job)
                except Exception:
                    pass
        except Exception:
            pass

        # rozłącz i zamknij port/log
        try:
            self.disconnect_serial()
        except Exception:
            pass

        # krótka pauza pozwalająca wątkom na zakończenie
        time.sleep(0.05)
        try:
            self.root.destroy()
        except Exception:
            try:
                self.root.quit()
            except Exception:
                pass


def main():
    root = tk.Tk()
    app = ModernSerialReaderApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()