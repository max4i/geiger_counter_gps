# geiger_v30_final.py - Ostateczna wersja z cache mapy i poprawionym raportem PDF

import os
import sys
import json
import threading
import queue
import time
import sqlite3
import hashlib
import io
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple, Dict, Any
from collections import deque
from queue import Queue, Empty

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from PIL import Image, ImageTk, ImageDraw, ImageFont
import requests

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

# NOWY: Map View dla interaktywnej mapy w oknie
try:
    from tkintermapview import TkinterMapView

    MAPVIEW_AVAILABLE = True
except ImportError:
    MAPVIEW_AVAILABLE = False

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


# ---------- cache kafelków mapy (UKRYTE) ----------
class MapTileCache:
    """Cache dla kafelków mapy - przechowuje kafelki lokalnie"""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        ensure_dir(cache_dir)
        self.db_path = os.path.join(cache_dir, "tile_cache.db")
        self.tile_dir = os.path.join(cache_dir, "tiles")
        ensure_dir(self.tile_dir)
        self._init_db()

        # Cache w pamięci RAM dla często używanych kafelków
        self.memory_cache = {}
        self.max_memory_cache = 300  # maksymalna liczba kafelków w pamięci

        # Statystyki
        self.hits = 0
        self.misses = 0
        self.downloads = 0

        # Kolejka do asynchronicznego pobierania
        self.download_queue = Queue()
        self.download_thread = None
        self._stop_downloader = threading.Event()
        self._start_downloader()

        # Referencja do widgetu mapy dla odświeżania
        self.map_widget_ref = None

    def _init_db(self):
        """Inicjalizacja bazy danych dla cache kafelków"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS tiles
                       (
                           tile_key
                           TEXT
                           PRIMARY
                           KEY,
                           url
                           TEXT
                           NOT
                           NULL,
                           tile_data
                           BLOB,
                           created
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           last_accessed
                           TIMESTAMP
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           access_count
                           INTEGER
                           DEFAULT
                           0
                       )
                       ''')

        cursor.execute('''
                       CREATE INDEX IF NOT EXISTS idx_tile_key ON tiles(tile_key)
                       ''')

        cursor.execute('''
                       CREATE INDEX IF NOT EXISTS idx_last_accessed ON tiles(last_accessed)
                       ''')

        conn.commit()
        conn.close()

    def _start_downloader(self):
        """Uruchomienie wątku do asynchronicznego pobierania kafelków"""
        self.download_thread = threading.Thread(
            target=self._download_worker,
            daemon=True
        )
        self.download_thread.start()

    def _download_worker(self):
        """Worker do pobierania kafelków w tle"""
        while not self._stop_downloader.is_set():
            try:
                url, tile_key, callback = self.download_queue.get(timeout=1)
                try:
                    response = requests.get(url, timeout=15, stream=True)
                    if response.status_code == 200:
                        # Zapisz do cache
                        tile_data = response.content
                        self._save_to_cache(tile_key, url, tile_data)
                        self.downloads += 1

                        # Wywołaj callback jeśli podany
                        if callback:
                            callback(tile_key, tile_data)

                    # Oznacz zadanie jako wykonane
                    self.download_queue.task_done()

                except requests.exceptions.Timeout:
                    print(f"[TILE CACHE] Timeout pobierania kafelka: {url}")
                    self.download_queue.task_done()
                except Exception as e:
                    print(f"[TILE CACHE] Błąd pobierania kafelka: {e}")
                    self.download_queue.task_done()

            except Empty:
                continue
            except Exception as e:
                if not self._stop_downloader.is_set():
                    print(f"[TILE CACHE] Błąd workera: {e}")
                break

    def stop(self):
        """Zatrzymuje cache"""
        self._stop_downloader.set()
        if self.download_thread and self.download_thread.is_alive():
            self.download_thread.join(timeout=2.0)

    def get_tile_key(self, url: str) -> str:
        """Generuje klucz cache dla URL kafelka"""
        return hashlib.md5(url.encode()).hexdigest()

    def get_tile(self, url: str, async_download: bool = True) -> Optional[bytes]:
        """Pobiera kafelek z cache lub z sieci"""
        tile_key = self.get_tile_key(url)

        # 1. Sprawdź cache w pamięci RAM
        if tile_key in self.memory_cache:
            self.hits += 1
            self._update_access_count(tile_key)
            return self.memory_cache[tile_key]

        # 2. Sprawdź cache w bazie danych
        tile_data = self._get_from_db_cache(tile_key)
        if tile_data:
            self.hits += 1
            self._add_to_memory_cache(tile_key, tile_data)
            return tile_data

        # 3. Jeśli nie ma w cache
        self.misses += 1

        if async_download:
            # Dodaj callback do aktualizacji mapy po pobraniu
            def tile_downloaded(key, data):
                # Ponowne renderowanie widoku
                if self.map_widget_ref:
                    try:
                        # Odśwież widok mapy
                        self.map_widget_ref._redraw_map()
                    except Exception:
                        pass

            self._queue_tile_download(url, tile_key, tile_downloaded)
            return self._create_placeholder_tile()
        else:
            # Spróbuj pobrać synchronicznie (tylko dla krytycznych kafelków)
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    tile_data = response.content
                    self._save_to_cache(tile_key, url, tile_data)
                    self.downloads += 1
                    return tile_data
            except Exception:
                pass

            return self._create_placeholder_tile()

    def _get_from_db_cache(self, tile_key: str) -> Optional[bytes]:
        """Pobiera kafelek z bazy danych"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                           SELECT tile_data
                           FROM tiles
                           WHERE tile_key = ?
                           ''', (tile_key,))

            row = cursor.fetchone()

            if row:
                # Aktualizuj licznik dostępu
                cursor.execute('''
                               UPDATE tiles
                               SET last_accessed = CURRENT_TIMESTAMP,
                                   access_count  = access_count + 1
                               WHERE tile_key = ?
                               ''', (tile_key,))
                conn.commit()

                tile_data = row[0]
                conn.close()
                return tile_data

            conn.close()
            return None

        except Exception as e:
            print(f"[TILE CACHE] Błąd odczytu z DB: {e}")
            return None

    def _save_to_cache(self, tile_key: str, url: str, tile_data: bytes):
        """Zapisuje kafelek do cache"""
        try:
            # Zapisz do bazy danych
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT OR REPLACE INTO tiles (tile_key, url, tile_data, access_count)
            VALUES (?, ?, ?, 1)
            ''', (tile_key, url, tile_data))

            conn.commit()
            conn.close()

            # Dodaj do cache w pamięci RAM
            self._add_to_memory_cache(tile_key, tile_data)

            # Oczyść stary cache jeśli za dużo
            if len(self.memory_cache) > self.max_memory_cache * 1.5:
                self._cleanup_old_tiles()

        except Exception as e:
            print(f"[TILE CACHE] Błąd zapisu do cache: {e}")

    def _add_to_memory_cache(self, tile_key: str, tile_data: bytes):
        """Dodaje kafelek do cache w pamięci RAM"""
        if len(self.memory_cache) >= self.max_memory_cache:
            # Usuń najstarszy kafelek (FIFO)
            if self.memory_cache:
                oldest_key = next(iter(self.memory_cache))
                del self.memory_cache[oldest_key]

        self.memory_cache[tile_key] = tile_data

    def _update_access_count(self, tile_key: str):
        """Aktualizuje licznik dostępu w bazie danych"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                           UPDATE tiles
                           SET last_accessed = CURRENT_TIMESTAMP,
                               access_count  = access_count + 1
                           WHERE tile_key = ?
                           ''', (tile_key,))

            conn.commit()
            conn.close()

        except Exception:
            pass

    def _queue_tile_download(self, url: str, tile_key: str, callback=None):
        """Dodaje kafelek do kolejki pobierania"""
        self.download_queue.put((url, tile_key, callback))

    def _create_placeholder_tile(self) -> bytes:
        """Tworzy szary placeholder dla brakujących kafelków"""
        try:
            # Stwórz szary obrazek 256x256
            img = Image.new('RGB', (256, 256), color='#e0e0e0')

            # Dodaj prosty tekst informacyjny
            draw = ImageDraw.Draw(img)

            # Spróbuj załadować czcionkę
            try:
                font = ImageFont.truetype("arial.ttf", 20)
            except:
                font = ImageFont.load_default()

            # Narysuj tekst
            text = "Ładowanie..."
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            position = ((256 - text_width) // 2, (256 - text_height) // 2)

            draw.text(position, text, fill="#808080", font=font)

            # Zapisz do bytes
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            return img_byte_arr.getvalue()

        except Exception as e:
            print(f"[TILE CACHE] Błąd tworzenia placeholder: {e}")
            # Fallback - puste bytes
            return b''

    def _cleanup_old_tiles(self, max_age_days: int = 30, max_tiles: int = 5000):
        """Czyści stare kafelki z cache"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Usuń kafelki starsze niż max_age_days
            cursor.execute(f'''
            DELETE FROM tiles 
            WHERE julianday('now') - julianday(created) > {max_age_days}
            ''')

            deleted = cursor.rowcount
            if deleted > 0:
                print(f"[TILE CACHE] Usunięto {deleted} starych kafelków")

            # Jeśli nadal za dużo, usuń najrzadziej używane
            cursor.execute('SELECT COUNT(*) FROM tiles')
            count = cursor.fetchone()[0]

            if count > max_tiles:
                to_delete = count - max_tiles
                cursor.execute('''
                               DELETE
                               FROM tiles
                               WHERE tile_key IN (SELECT tile_key
                                                  FROM tiles
                                                  ORDER BY last_accessed ASC, access_count ASC
                                   LIMIT ?
                                   )
                               ''', (to_delete,))

                print(f"[TILE CACHE] Usunięto {to_delete} najrzadziej używanych kafelków")

            conn.commit()
            conn.close()

            # Oczyść też memory cache
            if len(self.memory_cache) > self.max_memory_cache:
                excess = len(self.memory_cache) - self.max_memory_cache
                keys_to_remove = list(self.memory_cache.keys())[:excess]
                for key in keys_to_remove:
                    del self.memory_cache[key]

        except Exception as e:
            print(f"[TILE CACHE] Błąd czyszczenia cache: {e}")

    def clear_cache(self):
        """Czyści cały cache"""
        try:
            # Zatrzymaj downloader
            self.stop()

            # Wyczyść memory cache
            self.memory_cache.clear()

            # Wyczyść bazę danych
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tiles')
            conn.commit()
            conn.close()

            # Wyczyść statystyki
            self.hits = 0
            self.misses = 0
            self.downloads = 0

            # Uruchom ponownie downloader
            self._stop_downloader.clear()
            self._start_downloader()

            print("[TILE CACHE] Cache wyczyszczony")

        except Exception as e:
            print(f"[TILE CACHE] Błąd czyszczenia cache: {e}")


# ---------- MapTileCache INTEGRATION WITH TKINTERMAPVIEW ----------
class CachedTkinterMapView(TkinterMapView):
    """TkinterMapView with tile caching support"""

    def __init__(self, *args, tile_cache=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tile_cache = tile_cache

    def _get_image_from_url(self, url: str):
        """Override to use tile cache"""
        if self.tile_cache:
            tile_data = self.tile_cache.get_tile(url, async_download=True)
            if tile_data:
                try:
                    image = Image.open(io.BytesIO(tile_data))
                    return ImageTk.PhotoImage(image)
                except Exception as e:
                    print(f"[MAPVIEW] Błąd konwersji kafelka: {e}")
                    return None

        # Fallback to original method
        return super()._get_image_from_url(url)


# ---------- mock serial ----------
class MockSerial:
    """Mock serial device for testing without hardware"""

    def __init__(self, port=None, baudrate=1200, timeout=0.1):
        self.port = port or "MOCK_PORT"
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self._data_index = 0
        self._start_time = time.time()
        self._mock_positions = [
            (52.2297, 21.0122),  # Warszawa
            (52.2298, 21.0125),
            (52.2299, 21.0128),
            (52.2300, 21.0131),
            (52.2301, 21.0134),
        ]

    def read(self, size=1):
        """Generate mock data"""
        current_time = time.time()
        elapsed = current_time - self._start_time

        # Generate mock data every ~2 seconds
        if elapsed > 2:
            self._start_time = current_time
            self._data_index = (self._data_index + 1) % len(self._mock_positions)

            lat, lon = self._mock_positions[self._data_index]
            dose = 0.05 + (self._data_index * 0.05)  # Varying dose

            # Format: date|time|lat|lon|alt|sat|hdop|accuracy|current_dose|avg_dose
            mock_data = (
                f"01.01.2024|"
                f"{datetime.now().strftime('%H:%M:%S')}|"
                f"{lat:.6f}|"
                f"{lon:.6f}|"
                f"120|"
                f"8|"
                f"1.2|"
                f"5.0|"
                f"{dose:.3f}|"
                f"{dose:.3f}\n"
            )
            return mock_data.encode('utf-8')

        return b''

    @property
    def in_waiting(self):
        return 100 if time.time() - self._start_time > 2 else 0

    def close(self):
        self.is_open = False


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- konfiguracja ----------
class AppConfig:
    """Configuration manager with GUI support"""

    DEFAULT_CONFIG = {
        "serial": {
            "baudrate": 1200,
            "timeout": 0.1,
            "port": "",
            "use_mock": False
        },
        "display": {
            "history_hours": 4,
            "update_interval": 15,
            "plot_update_interval": 3.0,
            "theme": "light"
        },
        "alerts": {
            "threshold": 1.0,
            "levels": {
                "normal": {"min": 0.0, "max": 0.10, "emoji": "🟢", "color": "green"},
                "elevated": {"min": 0.10, "max": 0.25, "emoji": "🟡", "color": "yellow"},
                "warning": {"min": 0.25, "max": 1.0, "emoji": "🟠", "color": "orange"},
                "danger": {"min": 1.0, "max": float('inf'), "emoji": "🔴", "color": "red"}
            }
        },
        "filters": {
            "short_term_window": 16,
            "moving_avg_window": 5
        },
        "paths": {
            "log_dir": "C:/logi_geiger/" if sys.platform.startswith("win") else "./logi_geiger/",
            "map_dir": "",
            "resource_dir": "resources"
        },
        "map": {
            "default_tile_server": "Satelita",
            "default_zoom": 15,
            "default_lat": 52.2297,
            "default_lon": 21.0122,
            "cache_enabled": True  # Cache włączone domyślnie, ale ukryte z UI
        },
        "colors": {
            "bg_light": "#f0f0f0",
            "bg_dark": "#2d2d30",
            "accent": "#007acc",
            "success": "#107c10",
            "warning": "#d83b01",
            "danger": "#e81123",
            "text": "#323130"
        }
    }

    def __init__(self, config_file: str):
        self.config_file = config_file
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Load config from file or create default"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # Merge with defaults
                    config = self.DEFAULT_CONFIG.copy()
                    self._deep_update(config, loaded)
                    return config
        except Exception as e:
            print(f"[CONFIG] Błąd ładowania: {e}")

        return self.DEFAULT_CONFIG.copy()

    def save_config(self):
        """Save config to file"""
        try:
            ensure_dir(os.path.dirname(self.config_file))
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[CONFIG] Błąd zapisu: {e}")

    def _deep_update(self, target: Dict, source: Dict):
        """Recursively update nested dictionaries"""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_update(target[key], value)
            else:
                target[key] = value

    def get(self, key_path: str, default=None):
        """Get value by dot notation (e.g., 'serial.baudrate')"""
        keys = key_path.split('.')
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, key_path: str, value):
        """Set value by dot notation"""
        keys = key_path.split('.')
        config = self.config
        for key in keys[:-1]:
            if key not in config:
                config[key] = {}
            config = config[key]
        config[keys[-1]] = value
        self.save_config()


# ---------- aplikacja ----------
class ModernSerialReaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._is_closing = False

        # Konfiguracja przez klasę Config
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config = AppConfig(os.path.join(script_dir, "geiger_config.json"))

        # Pobieranie wartości z konfiguracji
        self.APP_TITLE = "Wer. 2.9 DRONE GPS GEIGER - 15LBOT"
        self.WINDOW_SIZE = "1200x800"
        self.MIN_WINDOW_SIZE = "1000x600"

        self.BAUDRATE = self.config.get("serial.baudrate", 1200)
        self.SERIAL_TIMEOUT = self.config.get("serial.timeout", 0.1)
        self.USE_MOCK = self.config.get("serial.use_mock", False)

        self.HISTORY_HOURS = self.config.get("display.history_hours", 4)
        self.UPDATE_INTERVAL = self.config.get("display.update_interval", 15)
        self.PLOT_UPDATE_MIN_INTERVAL = self.config.get("display.plot_update_interval", 3.0)

        # MAX_DATA_POINTS określane relatywnie do UPDATE_INTERVAL
        self.MAX_DATA_POINTS = max(1, (self.HISTORY_HOURS * 3600) // max(1, self.UPDATE_INTERVAL))

        # Poziomy dawki z konfiguracji
        dose_levels = self.config.get("alerts.levels", {})
        self.DOSE_LEVELS = {}
        for level, data in dose_levels.items():
            self.DOSE_LEVELS[level] = (
                data.get("min", 0.0),
                data.get("max", 0.0),
                data.get("emoji", "🟢"),
                data.get("color", "green")
            )

        # Filtrowanie danych z konfiguracji
        self.short_term_window = self.config.get("filters.short_term_window", 16)
        self.moving_avg_window = self.config.get("filters.moving_avg_window", 5)

        # ścieżki z konfiguracji
        self.LOG_DIR = self.config.get("paths.log_dir",
                                       os.path.abspath("C:/logi_geiger/") if sys.platform.startswith(
                                           "win") else os.path.abspath("./logi_geiger/"))
        self.MAP_DIR = os.path.join(self.LOG_DIR, "maps")
        self.RESOURCE_DIR = resource_path(self.config.get("paths.resource_dir", "resources"))
        self.CONFIG_FILE = os.path.join(self.LOG_DIR, "app_config.json")

        ensure_dir(self.LOG_DIR)
        ensure_dir(self.MAP_DIR)

        # kolory UI z konfiguracji
        self.COLORS = self.config.get("colors", {})

        # NOWE: Cache map (UKRYTE)
        self.CACHE_ENABLED = self.config.get("map.cache_enabled", True)

        if self.CACHE_ENABLED:
            self.tile_cache = MapTileCache(os.path.join(self.LOG_DIR, "tile_cache"))
        else:
            self.tile_cache = None

        # runtime variables
        self.serial_port = None
        self.read_thread: Optional[threading.Thread] = None
        self.reading_event = threading.Event()
        self.data_queue = queue.Queue()
        self.log_file = None
        self.log_filename = None

        self.current_data = GeigerData()
        self.historical_data: deque = deque(maxlen=5000)

        # użycie deque dla historii - automatyczne obcinanie
        self.raw_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.filtered_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.short_term_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.long_term_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.time_history = deque(maxlen=self.MAX_DATA_POINTS)

        # punkty alarmowe (trzymamy osobno)
        self.alarm_points: List[tuple] = []

        self.alarm_threshold = self.config.get("alerts.threshold", 1.0)  # μSv/h

        self.last_port = ""

        # NOWE ZMIENNE DLA TKINTERMAPVIEW
        self.map_widget: Optional[CachedTkinterMapView] = None
        self.follow_map_var = tk.BooleanVar(value=True)
        self.map_info_label: Optional[tk.Label] = None
        self.map_path_coords = []  # Lista krotek (lat, lon)
        self.map_path_object = None  # Obiekt ścieżki na mapie
        self.map_markers = []  # FIXED: Limited markers list
        self.temp_dose_marker = None  # Chwilowy marker (na 5 sekund)
        self.temp_marker_job = None  # ID joba do anulowania (dla zniknięcia markera)

        self.current_map_path = None  # Pozostawione dla Folium

        # rate-limit wykresu
        self._last_plot_update = 0.0

        # NEW: Configuration window reference
        self.config_window = None

        # Tile server configuration
        self.TILE_SERVERS = {
            "Satelita": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "OpenStreetMap": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "Teren": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
            "Ciemna": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
        }

        self.default_tile_server = self.config.get("map.default_tile_server", "Satelita")
        self.tile_var = tk.StringVar(value=self.default_tile_server)

        # init UI/plot
        self.load_last_port()
        self.setup_modern_ui()
        self.setup_plot()

        # pętla kolejki w GUI thread
        self._process_queue_job = self.root.after(100, self.process_queue)

    # ---------- konfiguracja ----------
    def load_last_port(self):
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    self.last_port = cfg.get('last_port', '')
                    if 'use_mock' in cfg:
                        self.USE_MOCK = cfg.get('use_mock', False)
        except Exception as e:
            print(f"[CONFIG] Błąd ładowania konfiguracji: {e}")

    def save_last_port(self):
        try:
            cfg = {
                'last_port': self.last_port,
                'use_mock': self.USE_MOCK
            }
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=4)
        except Exception as e:
            print(f"[CONFIG] Błąd zapisu konfiguracji: {e}")

    # ---------- GUI konfiguracji ----------
    def show_config_dialog(self):
        """Show configuration dialog window"""
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.lift()
            return

        self.config_window = tk.Toplevel(self.root)
        self.config_window.title("Konfiguracja aplikacji")
        self.config_window.geometry("600x500")
        self.config_window.resizable(False, False)

        # Notebook for config tabs
        config_notebook = ttk.Notebook(self.config_window)
        config_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Serial config tab
        serial_frame = ttk.Frame(config_notebook)
        config_notebook.add(serial_frame, text="Port szeregowy")

        ttk.Label(serial_frame, text="Baudrate:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        baudrate_var = tk.StringVar(value=str(self.BAUDRATE))
        ttk.Combobox(serial_frame, textvariable=baudrate_var,
                     values=['1200', '2400', '4800', '9600', '19200', '38400', '57600', '115200']).grid(
            row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(serial_frame, text="Timeout (s):").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        timeout_var = tk.StringVar(value=str(self.SERIAL_TIMEOUT))
        ttk.Entry(serial_frame, textvariable=timeout_var, width=10).grid(
            row=1, column=1, padx=5, pady=5, sticky=tk.W)

        # NEW: Mock mode checkbox
        mock_var = tk.BooleanVar(value=self.USE_MOCK)
        ttk.Checkbutton(serial_frame, text="Tryb symulacji (bez urządzenia)",
                        variable=mock_var).grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # Display config tab
        display_frame = ttk.Frame(config_notebook)
        config_notebook.add(display_frame, text="Wyświetlanie")

        ttk.Label(display_frame, text="Historia (godziny):").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        history_var = tk.StringVar(value=str(self.HISTORY_HOURS))
        ttk.Spinbox(display_frame, textvariable=history_var, from_=1, to=24, width=10).grid(
            row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(display_frame, text="Interwał mapy (s):").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        interval_var = tk.StringVar(value=str(self.UPDATE_INTERVAL))
        ttk.Spinbox(display_frame, textvariable=interval_var, from_=1, to=60, width=10).grid(
            row=1, column=1, padx=5, pady=5, sticky=tk.W)

        # Map config tab
        map_frame = ttk.Frame(config_notebook)
        config_notebook.add(map_frame, text="Mapa")

        ttk.Label(map_frame, text="Domyślna mapa:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        tile_var = tk.StringVar(value=self.default_tile_server)
        ttk.Combobox(map_frame, textvariable=tile_var,
                     values=list(self.TILE_SERVERS.keys()),
                     state="readonly", width=15).grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        # NEW: Cache checkbox (UKRYTE W KONFIGURACJI)
        cache_var = tk.BooleanVar(value=self.CACHE_ENABLED)
        ttk.Checkbutton(map_frame, text="Włącz cache mapy (szybsze ładowanie)",
                        variable=cache_var).grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # Alert config tab
        alert_frame = ttk.Frame(config_notebook)
        config_notebook.add(alert_frame, text="Alerty")

        ttk.Label(alert_frame, text="Próg alarmu (μSv/h):").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        alarm_var = tk.StringVar(value=str(self.alarm_threshold))
        ttk.Entry(alert_frame, textvariable=alarm_var, width=10).grid(
            row=0, column=1, padx=5, pady=5, sticky=tk.W)

        # Paths config tab
        paths_frame = ttk.Frame(config_notebook)
        config_notebook.add(paths_frame, text="Ścieżki")

        ttk.Label(paths_frame, text="Folder logów:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        log_dir_var = tk.StringVar(value=self.LOG_DIR)
        ttk.Entry(paths_frame, textvariable=log_dir_var, width=40).grid(
            row=0, column=1, padx=5, pady=5, sticky=tk.W)

        def browse_log_dir():
            folder = filedialog.askdirectory(initialdir=self.LOG_DIR)
            if folder:
                log_dir_var.set(folder)

        ttk.Button(paths_frame, text="Przeglądaj...", command=browse_log_dir).grid(
            row=0, column=2, padx=5, pady=5)

        # Buttons frame
        buttons_frame = ttk.Frame(self.config_window)
        buttons_frame.pack(fill=tk.X, padx=10, pady=10)

        def save_config():
            try:
                # Update serial config
                self.BAUDRATE = int(baudrate_var.get())
                self.SERIAL_TIMEOUT = float(timeout_var.get())
                self.USE_MOCK = mock_var.get()

                # Update display config
                self.HISTORY_HOURS = int(history_var.get())
                self.UPDATE_INTERVAL = int(interval_var.get())
                self.MAX_DATA_POINTS = max(1, (self.HISTORY_HOURS * 3600) // max(1, self.UPDATE_INTERVAL))

                # Update map config
                self.default_tile_server = tile_var.get()
                self.CACHE_ENABLED = cache_var.get()

                # Update alert config
                self.alarm_threshold = float(alarm_var.get())

                # Update paths
                self.LOG_DIR = log_dir_var.get()
                self.MAP_DIR = os.path.join(self.LOG_DIR, "maps")
                ensure_dir(self.LOG_DIR)
                ensure_dir(self.MAP_DIR)

                # Save to config file
                self.config.set("serial.baudrate", self.BAUDRATE)
                self.config.set("serial.timeout", self.SERIAL_TIMEOUT)
                self.config.set("serial.use_mock", self.USE_MOCK)
                self.config.set("display.history_hours", self.HISTORY_HOURS)
                self.config.set("display.update_interval", self.UPDATE_INTERVAL)
                self.config.set("map.default_tile_server", self.default_tile_server)
                self.config.set("map.cache_enabled", self.CACHE_ENABLED)
                self.config.set("alerts.threshold", self.alarm_threshold)
                self.config.set("paths.log_dir", self.LOG_DIR)

                # Reset data structures with new limits
                self.historical_data = deque(maxlen=5000)
                self.raw_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
                self.filtered_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
                self.short_term_history = deque(maxlen=self.MAX_DATA_POINTS)
                self.long_term_history = deque(maxlen=self.MAX_DATA_POINTS)
                self.time_history = deque(maxlen=self.MAX_DATA_POINTS)

                # Update cache if needed
                if self.CACHE_ENABLED and not self.tile_cache:
                    self.tile_cache = MapTileCache(os.path.join(self.LOG_DIR, "tile_cache"))
                elif not self.CACHE_ENABLED and self.tile_cache:
                    # Stop cache if disabled
                    self.tile_cache.stop()
                    self.tile_cache = None

                self.log_message("Konfiguracja zapisana")
                self.config_window.destroy()
                self.config_window = None

            except Exception as e:
                messagebox.showerror("Błąd", f"Nieprawidłowe dane: {e}")

        def cancel_config():
            self.config_window.destroy()
            self.config_window = None

        ttk.Button(buttons_frame, text="Zapisz", command=save_config).pack(side=tk.RIGHT, padx=5)
        ttk.Button(buttons_frame, text="Anuluj", command=cancel_config).pack(side=tk.RIGHT, padx=5)

        # Handle window close
        self.config_window.protocol("WM_DELETE_WINDOW", cancel_config)

    # ---------- filtrowanie ----------
    def apply_moving_average(self, new_value: float) -> float:
        """Dodaje surową wartość i zwraca przefiltrowaną (okno moving_avg_window)."""
        try:
            self.raw_dose_history.append(new_value)
            if len(self.raw_dose_history) >= self.moving_avg_window:
                avg = sum(list(self.raw_dose_history)[-self.moving_avg_window:]) / self.moving_avg_window
                return avg
            else:
                return new_value
        except Exception:
            return new_value

    def calculate_short_term_avg(self) -> float:
        if not self.filtered_dose_history:
            return 0.0
        window = list(self.filtered_dose_history)[-self.short_term_window:]
        return sum(window) / len(window)

    def calculate_long_term_avg(self) -> float:
        if not self.filtered_dose_history:
            return 0.0
        return sum(self.filtered_dose_history) / len(self.filtered_dose_history)

    # ---------- klasyfikacja dawek ----------
    def classify_dose(self, dose_value: float) -> Tuple[str, str, str]:
        """Zwraca (level_name, emoji, color)."""
        for level, (min_val, max_val, emoji, color) in self.DOSE_LEVELS.items():
            if min_val <= dose_value < max_val:
                return level, emoji, color
        return 'danger', '🔴', 'red'

    def get_dose_color(self, dose_value: float) -> str:
        """Zwraca nazwę koloru ('green', 'yellow', 'orange', 'red')."""
        _, _, color = self.classify_dose(dose_value)
        return color

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

        self.create_control_panel(main_container)
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

        # NEW: Config button
        ttk.Button(control_frame, text="Konfiguracja...", command=self.show_config_dialog).pack(fill=tk.X, pady=5)

        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, pady=10)
        ttk.Label(status_frame, text="Status:").pack(anchor=tk.W)
        status_text = "Tryb symulacji" if self.USE_MOCK else "Niepołączono"
        self.status_label = ttk.Label(status_frame, text=status_text,
                                      foreground="orange" if self.USE_MOCK else "red",
                                      font=('Segoe UI', 9, 'bold'))
        self.status_label.pack(anchor=tk.W)

        ttk.Separator(control_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        ttk.Label(control_frame, text="Szybkie akcje:", font=('Segoe UI', 9, 'bold')).pack(anchor=tk.W)

        # Przycisk do eksportu Folium do przeglądarki
        self.map_btn = ttk.Button(control_frame, text="Eksportuj mapę (HTML)", command=self.generate_and_show_map,
                                  state=tk.DISABLED)
        self.map_btn.pack(fill=tk.X, pady=5)

        ttk.Button(control_frame, text="Resetuj wykres", command=self.reset_plot).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Otwórz folder logów", command=self.open_log_folder).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Eksportuj dane (CSV)", command=self.export_data).pack(fill=tk.X, pady=5)
        ttk.Button(control_frame, text="Eksportuj dane (KML)", command=self.export_kml).pack(fill=tk.X, pady=5)

        # NEW: Export PDF button
        ttk.Button(control_frame, text="Eksportuj raport (PDF)", command=self.export_pdf_report).pack(fill=tk.X, pady=5)

        # logo - oba logo
        logo_frame = ttk.Frame(control_frame)
        logo_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        self.logo1_photo = None
        self.logo2_photo = None
        self._load_logos(logo_frame)

        self.refresh_ports()

    def _load_logos(self, parent):
        # Najpierw 15lbot.jpg, potem logo.jpg
        logos = [("15lbot.jpg", "logo1_photo"), ("logo.jpg", "logo2_photo")]
        for fname, attr in logos:
            try:
                p = resource_path(fname)
                if os.path.exists(p):
                    img = Image.open(p).convert("RGBA")
                    img = img.resize((120, 120), Image.LANCZOS)
                    datas = img.getdata()
                    new_data = []
                    for item in datas:
                        if item[0] > 240 and item[1] > 240 and item[2] > 240:
                            new_data.append((255, 255, 255, 0))
                        else:
                            new_data.append(item)
                    img.putdata(new_data)
                    photo = ImageTk.PhotoImage(img)
                    setattr(self, attr, photo)
                    lbl = tk.Label(parent, image=photo, bg=self.COLORS['bg_light'])
                    lbl.pack(pady=(0, 5))
                else:
                    print(f"[LOGO] Plik nie znaleziony: {fname}")
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

        graph_frame = ttk.LabelFrame(monitor_tab, text=f" Historia dawki - Ostatnie {self.HISTORY_HOURS} godziny ",
                                     padding=10)
        graph_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.graph_container = ttk.Frame(graph_frame)
        self.graph_container.pack(fill=tk.BOTH, expand=True)

        stats_frame = ttk.LabelFrame(monitor_tab, text=" Statystyki ", padding=10)
        stats_frame.pack(fill=tk.X)
        self.create_stats_grid(stats_frame)

    def create_data_grid(self, parent):
        dose_frame = ttk.Frame(parent)
        dose_frame.pack(fill=tk.X, pady=5)

        self.current_dose_var = tk.StringVar(value="0.00 μSv")
        self.short_term_dose_var = tk.StringVar(value="0.00 μSv/h")
        self.long_term_dose_var = tk.StringVar(value="0.00 μSv/h")
        self.short_term_dose_r_var = tk.StringVar(value="(0.00 mR/h)")

        ttk.Label(dose_frame, text="Dawka chwilowa:", font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(dose_frame, textvariable=self.current_dose_var, font=('Segoe UI', 12)).pack(side=tk.LEFT,
                                                                                              padx=(0, 30))

        ttk.Label(dose_frame, text="Średnia chwilowa:", font=('Segoe UI', 10)).pack(side=tk.LEFT, padx=(0, 10))
        self.short_term_dose_label = ttk.Label(dose_frame, textvariable=self.short_term_dose_var,
                                               font=('Segoe UI', 24))
        self.short_term_dose_label.pack(side=tk.LEFT, padx=(0, 10))

        self.short_term_dose_r_label = ttk.Label(dose_frame, textvariable=self.short_term_dose_r_var,
                                                 font=('Segoe UI', 14))
        self.short_term_dose_r_label.pack(side=tk.LEFT)

        gps_frame = ttk.Frame(parent)
        gps_frame.pack(fill=tk.X, pady=5)
        gps_frame.columnconfigure(0, weight=1)
        gps_frame.columnconfigure(1, weight=1)
        gps_frame.columnconfigure(2, weight=1)
        gps_frame.columnconfigure(3, weight=1)

        pos_frame = ttk.LabelFrame(gps_frame, text=" Pozycja ", padding=5)
        pos_frame.grid(row=0, column=0, padx=5, sticky="ew")
        self.lat_var = tk.StringVar(value="N: 00.000000")
        self.lon_var = tk.StringVar(value="E: 00.000000")
        ttk.Label(pos_frame, textvariable=self.lat_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(pos_frame, textvariable=self.lon_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

        time_frame = ttk.LabelFrame(gps_frame, text=" Czas ", padding=5)
        time_frame.grid(row=0, column=1, padx=5, sticky="ew")
        self.date_var = tk.StringVar(value="Data: 00.00.00r")
        self.time_var = tk.StringVar(value="Czas Zulu: 00:00:00")
        ttk.Label(time_frame, textvariable=self.date_var, font=('Segoe UI', 9)).pack(anchor=tk.W)
        ttk.Label(time_frame, textvariable=self.time_var, font=('Segoe UI', 9)).pack(anchor=tk.W)

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
        for i in range(5):
            stats_frame.columnconfigure(i, weight=1)

        self.min_dose_var = tk.StringVar(value="Min: 0.00")
        self.max_dose_var = tk.StringVar(value="Max: 0.00")
        self.avg_dose_var = tk.StringVar(value="Śr. globalna: 0.00")
        self.points_var = tk.StringVar(value="Punkty: 0")
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
        self.notebook.add(self.map_tab, text="Mapa (Live)")

        # --- Panel sterowania mapą (góra) ---
        map_control_frame = ttk.Frame(self.map_tab)
        map_control_frame.pack(fill=tk.X, pady=5)

        # Przycisk eksportu do przeglądarki (stare Folium)
        ttk.Button(map_control_frame, text="Eksportuj do HTML (Folium)",
                   command=self.generate_and_show_map, state=tk.DISABLED if not FOLIUM_AVAILABLE else tk.NORMAL).pack(
            side=tk.LEFT, padx=5)

        # Przycisk otwierania w przeglądarce
        ttk.Button(map_control_frame, text="Otwórz ostatni eksport w przeglądarce",
                   command=self.open_map_in_browser).pack(side=tk.LEFT, padx=5)

        # Checkbox do śledzenia (centrowania na dronie)
        ttk.Checkbutton(map_control_frame, text="Śledź pozycję (Auto-centrowanie)",
                        variable=self.follow_map_var).pack(side=tk.LEFT, padx=10)

        # Tile server selector
        tile_frame = ttk.Frame(map_control_frame)
        tile_frame.pack(side=tk.LEFT, padx=10)
        ttk.Label(tile_frame, text="Mapa:").pack(side=tk.LEFT)
        tile_combo = ttk.Combobox(tile_frame, textvariable=self.tile_var,
                                  values=list(self.TILE_SERVERS.keys()),
                                  state="readonly", width=12)
        tile_combo.pack(side=tk.LEFT, padx=5)
        tile_combo.bind('<<ComboboxSelected>>', self.change_tile_server)

        # --- Główny kontener mapy ---
        map_container = ttk.Frame(self.map_tab)
        map_container.pack(fill=tk.BOTH, expand=True)

        if not MAPVIEW_AVAILABLE:
            ttk.Label(map_container,
                      text="Brak biblioteki tkintermapview.\nZainstaluj: py -m pip install tkintermapview",
                      foreground="red").pack(expand=True)
            self.map_widget = None
            return

        # --- Widget Mapy ---
        if self.CACHE_ENABLED and self.tile_cache:
            # Użyj naszej zmodyfikowanej klasy z cache
            self.map_widget = CachedTkinterMapView(map_container, width=800, height=600,
                                                   corner_radius=0, tile_cache=self.tile_cache)
            # Ustaw referencję w cache do widgetu mapy
            self.tile_cache.map_widget_ref = self.map_widget
        else:
            # Zwykły widget bez cache
            self.map_widget = TkinterMapView(map_container, width=800, height=600, corner_radius=0)

        self.map_widget.pack(fill="both", expand=True)

        # Ustaw domyślną mapę satelitarną
        default_server = self.TILE_SERVERS.get(self.default_tile_server,
                                               "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")
        self.map_widget.set_tile_server(default_server)
        self.map_widget.set_zoom(15)

        # Ustawienie domyślne na Polskę (Warszawa)
        default_lat = self.config.get("map.default_lat", 52.2297)
        default_lon = self.config.get("map.default_lon", 21.0122)
        self.map_widget.set_position(default_lat, default_lon)

        # --- Pływająca Legenda (Overlay) - Lewy Dół ---
        self.legend_frame = tk.Frame(self.map_widget, bg="white", bd=2, relief=tk.RAISED)
        self.legend_frame.place(relx=0.02, rely=0.98, anchor="sw")

        lbl_font = ('Segoe UI', 8)
        tk.Label(self.legend_frame, text="LEGENDA DAWKI", bg="white", font=('Segoe UI', 9, 'bold')).pack(anchor="w",
                                                                                                         padx=5, pady=2)
        tk.Label(self.legend_frame, text="● < 0.10 μSv/h (Norma)", fg="green", bg="white", font=lbl_font).pack(
            anchor="w", padx=5)
        tk.Label(self.legend_frame, text="● 0.10 - 0.25 μSv/h", fg="#b5b500", bg="white", font=lbl_font).pack(
            anchor="w", padx=5)
        tk.Label(self.legend_frame, text="● 0.25 - 1.00 μSv/h", fg="orange", bg="white", font=lbl_font).pack(anchor="w",
                                                                                                             padx=5)
        tk.Label(self.legend_frame, text="● > 1.00 μSv/h (Alarm)", fg="red", bg="white", font=lbl_font).pack(anchor="w",
                                                                                                             padx=5)
        tk.Label(self.legend_frame, text="--- Trasa pomiarów", fg="blue", bg="white", font=lbl_font).pack(anchor="w",
                                                                                                          padx=5)
        tk.Label(self.legend_frame, text="◼ Chwilowy pomiar (5s)", fg="black", bg="white", font=lbl_font).pack(
            anchor="w", padx=5)

        # --- Pływający Panel Info Ostatniego Punktu (Overlay) - Prawy Góra ---
        self.info_frame = tk.Frame(self.map_widget, bg="white", bd=2, relief=tk.RAISED)
        self.info_frame.place(relx=0.98, rely=0.02, anchor="ne")

        tk.Label(self.info_frame, text="OSTATNI POMIAR", bg="white", font=('Segoe UI', 9, 'bold')).pack(anchor="w",
                                                                                                        padx=5, pady=2)
        self.map_info_label = tk.Label(self.info_frame, text="Czekam na dane GPS...", bg="white", font=('Consolas', 9),
                                       justify=tk.LEFT)
        self.map_info_label.pack(padx=5, pady=5)

    def change_tile_server(self, event=None):
        """Change map tile server"""
        if not self.map_widget or self._is_closing:
            return

        selection = self.tile_var.get()
        tile_server = self.TILE_SERVERS.get(selection)

        if tile_server:
            self.map_widget.set_tile_server(tile_server)
            self.log_message(f"Zmieniono mapę na: {selection}")

            # Zapisz wybór w konfiguracji
            self.config.set("map.default_tile_server", selection)

    def create_logs_tab(self):
        logs_tab = ttk.Frame(self.notebook)
        self.notebook.add(logs_tab, text="Logi")

        log_control_frame = ttk.Frame(logs_tab)
        log_control_frame.pack(fill=tk.X, pady=5)
        ttk.Button(log_control_frame, text="Wyczyść logi", command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_control_frame, text="Zapisz logi", command=self.save_logs).pack(side=tk.LEFT, padx=5)

        self.log_text = scrolledtext.ScrolledText(logs_tab, wrap=tk.WORD, width=80, height=20, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ---------- wykres ----------
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
        # re-inicjalizacja deque z aktualnym MAX_DATA_POINTS
        self.raw_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.filtered_dose_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.short_term_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.long_term_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.time_history = deque(maxlen=self.MAX_DATA_POINTS)
        self.alarm_points.clear()

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
        self.ax.set_title(f"Historia dawki - Ostatnie {self.HISTORY_HOURS} godziny", fontsize=10, pad=8)
        self.canvas.draw()
        self.log_message("Wykres zresetowany")

    # ---------- serial ----------
    def refresh_ports(self):
        values = []
        try:
            if SERIAL_AVAILABLE and not self.USE_MOCK:
                ports = serial.tools.list_ports.comports()
                values = [f"{p.device} - {p.description}" for p in ports]
            else:
                values = ["MOCK_PORT - Tryb symulacji"]
        except Exception as e:
            self.log_message(f"Błąd listowania portów: {e}")
            values = []

        self.port_combobox['values'] = values
        if values:
            if self.last_port and not self.USE_MOCK:
                for v in values:
                    if self.last_port in v:
                        self.port_combobox.set(v)
                        break
                else:
                    self.port_combobox.set(values[0])
            else:
                self.port_combobox.set(values[0])

    def connect_serial(self):
        # NEW: Handle mock mode
        if self.USE_MOCK:
            try:
                self.serial_port = MockSerial()
                self.last_port = "MOCK_PORT"
                self.save_last_port()
                self.open_log_file()
                self.reading_event.set()
                self.read_thread = threading.Thread(target=self._serial_read_loop, daemon=True)
                self.read_thread.start()

                self.connect_btn.config(state=tk.DISABLED)
                self.disconnect_btn.config(state=tk.NORMAL)
                self.port_combobox.config(state=tk.DISABLED)
                self.status_label.config(text="Tryb symulacji (Połączono)", foreground="green")
                self.map_btn.config(state=tk.NORMAL)

                self.log_message("Połączono w trybie symulacji")
                return
            except Exception as e:
                messagebox.showerror("Błąd", f"Nie można uruchomić symulacji: {e}")
                return

        # Original serial connection logic
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
        try:
            # Ustaw flagę zamykania
            self._is_closing = True

            # Zatrzymaj wątek odczytu
            self.reading_event.clear()

            # Poczekaj na zakończenie wątku
            if self.read_thread and self.read_thread.is_alive():
                try:
                    self.read_thread.join(timeout=1.0)
                except Exception:
                    pass

            # Zamknij port szeregowy
            if self.serial_port and getattr(self.serial_port, "is_open", False):
                try:
                    self.serial_port.close()
                except Exception:
                    pass

            # Zamknij plik logu
            self.close_log_file()

            # Zaktualizuj interfejs
            self.connect_btn.config(state=tk.NORMAL)
            self.disconnect_btn.config(state=tk.DISABLED)
            self.port_combobox.config(state=tk.NORMAL)
            self.status_label.config(text="Rozłączono", foreground="red")
            self.map_btn.config(state=tk.DISABLED)

            # Anuluj zaplanowane zadania
            if self.temp_marker_job:
                try:
                    self.root.after_cancel(self.temp_marker_job)
                except Exception:
                    pass
                self.temp_marker_job = None

            # Usuń tymczasowy marker
            if self.temp_dose_marker and self.map_widget and not self._is_closing:
                try:
                    self.temp_dose_marker.delete()
                except Exception:
                    pass
                self.temp_dose_marker = None

            # Wyczyść mapę
            if self.map_widget and not self._is_closing:
                try:
                    if self.map_path_object:
                        self.map_path_object.delete()
                        self.map_path_object = None

                    for marker in self.map_markers:
                        try:
                            marker.delete()
                        except Exception:
                            pass
                    self.map_markers.clear()

                    self.map_path_coords = []

                    if self.map_info_label:
                        self.map_info_label.config(text="Czekam na dane GPS...")

                except Exception as e:
                    if "invalid command name" not in str(e):
                        self.log_message(f"Błąd przy czyszczeniu mapy: {e}")

            # Zatrzymaj tile cache jeśli istnieje
            if self.tile_cache:
                self.tile_cache.stop()

            disconnect_msg = "Rozłączono z portu szeregowego" if not self.USE_MOCK else "Zatrzymano symulację"
            self.log_message(disconnect_msg)

            # Zresetuj flagę zamykania
            self._is_closing = False

        except Exception as e:
            if "invalid command name" not in str(e):
                self.log_message(f"Błąd przy rozłączaniu: {e}")

    def _serial_read_loop(self):
        buffer = ""
        while self.reading_event.is_set() and not self._is_closing:
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
                            if not self._is_closing:
                                self.data_queue.put(('data', line))
                else:
                    time.sleep(0.05)
            except Exception as e:
                if not self._is_closing:
                    try:
                        self.data_queue.put(('error', f"Błąd komunikacji: {e}"))
                    except Exception:
                        pass
                break

    # ---------- przetwarzanie kolejki ----------
    def process_queue(self):
        if self._is_closing:
            return

        try:
            while True:
                msg_type, payload = self.data_queue.get_nowait()
                if msg_type == 'data':
                    self.process_serial_data(payload)
                elif msg_type == 'error':
                    self.log_message(payload)
                    try:
                        messagebox.showerror("Błąd", payload)
                    except Exception:
                        pass
        except queue.Empty:
            pass

        if not self._is_closing:
            self._process_queue_job = self.root.after(100, self.process_queue)

    def process_serial_data(self, line: str):
        if self._is_closing:
            return

        self.log_message(line)
        self.write_to_log(line)

        g = self.parse_data(line)
        if not g:
            return

        try:
            current_dose = safe_float(g.current_dose, 0.0)
            filtered_dose = self.apply_moving_average(current_dose)
            self._append_history_point(g, filtered_dose)
        except Exception as e:
            self.log_message(f"Błąd przy filtrowaniu/appendzie: {e}")
            filtered_dose = safe_float(g.current_dose, 0.0)

        try:
            self.update_display(g, filtered_dose)
        except Exception as e:
            if "invalid command name" not in str(e):
                self.log_message(f"Błąd aktualizacji widoku: {e}")

        try:
            now = time.time()
            if now - self._last_plot_update >= self.PLOT_UPDATE_MIN_INTERVAL:
                self.update_plot()
                self._last_plot_update = now
            else:
                self.update_stats()
        except Exception as e:
            self.log_message(f"Błąd aktualizacji wykresu/statystyk: {e}")

        # Aktualizacja mapy live
        lat = safe_float(g.latitude)
        lon = safe_float(g.longitude)
        if lat != 0.0 and lon != 0.0:
            self.update_realtime_map(g, filtered_dose)

    # ---------- parsing ----------
    def parse_data(self, data: str) -> Optional[GeigerData]:
        try:
            parts = data.split('|')
            if len(parts) < 10:
                return None
            date_s = parts[0].strip()
            time_s = parts[1].strip()
            timestamp = self._parse_gps_datetime_safe(date_s, time_s)
            gd = GeigerData(
                date=date_s,
                time=time_s,
                latitude=parts[2].strip(),
                longitude=parts[3].strip(),
                altitude=parts[4].strip() if len(parts) > 4 else "0",
                satellites=parts[5].strip() if len(parts) > 5 else "0",
                hdop=parts[6].strip() if len(parts) > 6 else "0",
                accuracy=parts[7].strip() if len(parts) > 7 else "0",
                current_dose=parts[8].strip() if len(parts) > 8 else "0.00",
                average_dose=parts[9].strip() if len(parts) > 9 else "0.00",
                timestamp=timestamp
            )
            self.historical_data.append(gd)
            return gd
        except Exception as e:
            self.log_message(f"Błąd parsowania: {e}")
            return None

    def _parse_gps_datetime_safe(self, date_str: str, time_str: str) -> datetime:
        candidates = []
        if date_str and time_str:
            candidates.append(f"{date_str} {time_str}")
            try:
                parts = date_str.split('.')
                if len(parts) == 3 and len(parts[2]) == 2:
                    yy = int(parts[2])
                    year_full = 2000 + yy if yy < 70 else 1900 + yy
                    candidates.append(f"{parts[0]}.{parts[1]}.{year_full} {time_str}")
            except Exception:
                pass

        formats = ["%d.%m.%Y %H:%M:%S", "%d.%m.%y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"]
        for candidate in candidates:
            for fmt in formats:
                try:
                    return datetime.strptime(candidate, fmt)
                except Exception:
                    continue
        return datetime.now()

    # ---------- widok ----------
    def update_display(self, data: GeigerData, filtered_dose: float):
        if self._is_closing:
            return

        self.current_data = data

        short_term_avg = self.calculate_short_term_avg()
        long_term_avg = self.calculate_long_term_avg()

        _, _, color = self.classify_dose(short_term_avg)

        try:
            self.short_term_dose_label.config(foreground=color)
            self.short_term_dose_r_label.config(foreground=color)

            dose_mr_value = short_term_avg * 0.1
            daily_dose_value = short_term_avg * 24
            daily_mr_value = dose_mr_value * 24

            self.current_dose_var.set(f"{filtered_dose:.2f} μSv")
            self.short_term_dose_var.set(f"{short_term_avg:.2f} μSv/h")
            self.short_term_dose_r_var.set(f"({dose_mr_value:.2f} mR/h)")

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
        except Exception as e:
            if "invalid command name" not in str(e):
                raise e

    def _append_history_point(self, g: GeigerData, filtered_dose: float):
        try:
            t = g.timestamp if g.timestamp else self._parse_gps_datetime_safe(g.date, g.time)
            self.time_history.append(t)
            self.filtered_dose_history.append(filtered_dose)

            short_term_avg = self.calculate_short_term_avg()
            long_term_avg = self.calculate_long_term_avg()

            self.short_term_history.append(short_term_avg)
            self.long_term_history.append(long_term_avg)

            if filtered_dose > self.alarm_threshold:
                self.alarm_points.append((t, filtered_dose))
                if len(self.alarm_points) > self.MAX_DATA_POINTS * 2:
                    self.alarm_points = self.alarm_points[-int(self.MAX_DATA_POINTS * 2):]
        except Exception as e:
            self.log_message(f"Błąd dodawania punktu historii: {e}")

    def update_plot(self):
        if self._is_closing:
            return

        try:
            self.ax.clear()
            if self.filtered_dose_history and self.time_history:
                times_num = [mdates.date2num(t) for t in self.time_history]

                if len(self.filtered_dose_history) > 0 and len(self.filtered_dose_history) == len(times_num):
                    if len(times_num) > 1:
                        time_diff = times_num[-1] - times_num[0]
                        width = (time_diff / len(times_num)) * 0.6
                    else:
                        width = 1 / 1440.0
                    self.ax.bar(times_num, list(self.filtered_dose_history), width=width,
                                align='center', alpha=0.3, color='lightgray',
                                edgecolor='gray', linewidth=0.5,
                                label='Wartości chwilowe')

                if len(self.long_term_history) > 0 and len(self.long_term_history) == len(times_num):
                    self.ax.plot(times_num, list(self.long_term_history),
                                 color='blue', linewidth=2,
                                 label='Średnia globalna')

                if len(self.short_term_history) > 0 and len(self.short_term_history) == len(times_num):
                    self.ax.plot(times_num, list(self.short_term_history),
                                 color='orange', linewidth=2, linestyle='--',
                                 label='Średnia chwilowa')

                if self.alarm_points:
                    alarm_times, alarm_values = zip(*self.alarm_points)
                    alarm_times_num = [mdates.date2num(t) for t in alarm_times]
                    self.ax.scatter(alarm_times_num, alarm_values,
                                    color='red', s=50, zorder=5,
                                    label=f'Alarm (> {self.alarm_threshold} μSv/h)')

                self.ax.legend(loc='upper right', fontsize=8)

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

                all_values = []
                all_values.extend(list(self.filtered_dose_history) or [])
                all_values.extend(list(self.long_term_history) or [])
                all_values.extend(list(self.short_term_history) or [])
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
        if self._is_closing:
            return

        if self.filtered_dose_history:
            mn = min(self.filtered_dose_history)
            mx = max(self.filtered_dose_history)
            avg_global = sum(self.filtered_dose_history) / len(self.filtered_dose_history)

            avg_short_term = self.short_term_history[-1] if self.short_term_history else 0.0

            self.min_dose_var.set(f"Min: {mn:.2f}")
            self.max_dose_var.set(f"Max: {mx:.2f}")
            self.avg_dose_var.set(f"Śr. globalna: {avg_global:.2f}")
            self.short_term_avg_var.set(f"Śr. chwilowa: {avg_short_term:.2f}")
            self.points_var.set(f"Punkty: {len(self.filtered_dose_history)}")
        else:
            self.min_dose_var.set("Min: 0.00")
            self.max_dose_var.set("Max: 0.00")
            self.avg_dose_var.set("Śr. globalna: 0.00")
            self.short_term_avg_var.set("Śr. chwilowa: 0.00")
            self.points_var.set("Punkty: 0")

    # ---------- mapa (NOWA LOGIKA) ----------

    def _clear_temp_marker(self):
        """Usuwa chwilowy marker po upływie 5 sekund."""
        if self.temp_dose_marker and self.map_widget and not self._is_closing:
            try:
                self.temp_dose_marker.delete()
            except Exception:
                pass
            self.temp_dose_marker = None
        self.temp_marker_job = None

    def update_realtime_map(self, data: GeigerData, dose_val: float):
        """Metoda aktualizująca widok mapy w czasie rzeczywistym używając tkintermapview"""
        if self._is_closing:
            return

        if not MAPVIEW_AVAILABLE or not self.map_widget:
            return

        try:
            lat = safe_float(data.latitude)
            lon = safe_float(data.longitude)

            # Wymagane, żeby nie rysować punktu na (0,0)
            if lat == 0.0 and lon == 0.0:
                return

            # Wymagane kolory i teksty
            color_name, emoji, color_fg = self.classify_dose(dose_val)
            hex_colors = {
                'green': 'green',
                'yellow': '#b5b500',
                'orange': 'orange',
                'red': 'red'
            }
            marker_color = hex_colors.get(color_name, 'red')

            # Tekst do popupa/detali
            marker_text = (
                f"{emoji} {dose_val:.3f} μSv/h ({color_name.upper()})\n"
                f"Czas: {data.time} | Data: {data.date}\n"
                f"GPS: {lat:.6f}, {lon:.6f} | Alt: {data.altitude}m"
            )

            # 1. Rysowanie Pełnej Trasy (Linii)
            self.map_path_coords.append((lat, lon))

            if len(self.map_path_coords) >= 1:
                if self.map_path_object:
                    try:
                        self.map_path_object.delete()
                    except Exception:
                        pass

                try:
                    self.map_path_object = self.map_widget.set_path(self.map_path_coords, color="blue", width=3)
                except Exception:
                    pass

            # 2. Chwilowy Marker (na 5 sekund)
            if self.temp_marker_job:
                try:
                    self.root.after_cancel(self.temp_marker_job)
                except Exception:
                    pass
                self.temp_marker_job = None

            if self.temp_dose_marker:
                try:
                    self.temp_dose_marker.delete()
                except Exception:
                    pass
                self.temp_dose_marker = None

            try:
                self.temp_dose_marker = self.map_widget.set_marker(
                    lat, lon,
                    text=f"NOWY POMIAR: {dose_val:.3f} μSv/h",
                    marker_color_circle='black',
                    marker_color_outside='black',
                    text_color="black",
                    font=("arial", 11, 'bold')
                )
            except Exception:
                self.temp_dose_marker = None

            if not self._is_closing:
                self.temp_marker_job = self.root.after(5000, self._clear_temp_marker)

            # 3. Stały Marker Ostatniego Punktu
            if len(self.map_markers) > 100:
                old_marker = self.map_markers.pop(0)
                try:
                    old_marker.delete()
                except Exception:
                    pass

            try:
                main_marker = self.map_widget.set_marker(
                    lat, lon,
                    text=f"{dose_val:.2f} μSv/h",
                    marker_color_circle=marker_color,
                    marker_color_outside=marker_color,
                    text_color="white" if color_name == 'red' else "black",
                    font=("arial", 8),
                    command=lambda x=None: messagebox.showinfo("Szczegóły Punktu", marker_text)
                )
                self.map_markers.append(main_marker)
            except Exception:
                pass

            # 4. Aktualizacja Ramki Info
            info_text = (
                f"Czas: {data.time} | Data: {data.date}\n"
                f"Dawka: {dose_val:.3f} μSv/h ({color_name.upper()})\n"
                f"Lat:  {lat:.6f} | Lon: {lon:.6f}\n"
                f"Alt:  {data.altitude}m | Sat: {data.satellites}\n"
                f"HDOP: {data.hdop} | Acc: {data.accuracy}m"
            )
            if self.map_info_label:
                try:
                    self.map_info_label.config(text=info_text, foreground=marker_color)
                except Exception:
                    pass

            # 5. Auto-centrowanie
            if self.follow_map_var.get() and self.map_widget:
                try:
                    self.map_widget.set_position(lat, lon)
                except Exception:
                    pass

        except Exception as e:
            if "invalid command name" not in str(e):
                self.log_message(f"Błąd aktualizacji mapy live: {e}")

    def generate_and_show_map(self):
        if not FOLIUM_AVAILABLE:
            messagebox.showwarning("Uwaga", "Folium nie jest zainstalowane. Zainstaluj: py -m pip install folium")
            return
        if not self.historical_data:
            messagebox.showinfo("Info", "Brak danych do wygenerowania mapy")
            return

        try:
            self.log_message("Rozpoczynanie generowania mapy Folium...")
            self.root.update_idletasks()

            valid_points = self._collect_valid_map_points()
            if not valid_points:
                messagebox.showinfo("Info", "Brak prawidłowych danych GPS dla mapy")
                return

            center = self._calculate_center(valid_points)
            m = folium.Map(location=center, zoom_start=15, tiles='OpenStreetMap')

            points_added, line_points = self._add_points_to_map(m, valid_points)
            if points_added == 0:
                messagebox.showinfo("Info", "Nie udało się dodać żadnych punktów do mapy")
                return

            if len(line_points) >= 2:
                folium.PolyLine(locations=line_points, color='blue', weight=3, opacity=0.6,
                                tooltip="Trasa pomiarów").add_to(m)

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
            self.current_map_path = map_filename
            m.save(map_filename)

            self.log_message(f"Wygenerowano mapę Folium: {map_filename}")

            try:
                import webbrowser
                webbrowser.open(f'file://{os.path.abspath(map_filename)}')
            except Exception:
                pass

            messagebox.showinfo("Sukces",
                                f"Mapa wygenerowana pomyślnie i otwarta w przeglądarce!\n{points_added} punktów pomiarowych")

        except Exception as e:
            self.log_message(f"Błąd generowania mapy Folium: {e}")
            messagebox.showerror("Błąd", f"Nie udało się wygenerować mapy: {e}")

    def _collect_valid_map_points(self) -> List[GeigerData]:
        valid = []
        for d in self.historical_data:
            try:
                lat = float(d.latitude)
                lon = float(d.longitude)
                if lat == 0.0 and lon == 0.0:
                    continue
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
                dose = safe_float(d.average_dose)
                if lat == 0.0 and lon == 0.0:
                    continue
                line_points.append([lat, lon])

                _, _, color = self.classify_dose(dose)

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

    def open_map_in_browser(self):
        if self.current_map_path and os.path.exists(self.current_map_path):
            try:
                import webbrowser
                webbrowser.open(f'file://{os.path.abspath(self.current_map_path)}')
                self.log_message(f"Otwarto mapę: {self.current_map_path}")
            except Exception as e:
                self.log_message(f"Błąd otwierania mapy: {e}")
        else:
            messagebox.showinfo("Info", "Najpierw wygeneruj mapę (Eksportuj do HTML)")

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
                    dose = safe_float(d.average_dose)
                    if lat == 0.0 and lon == 0.0:
                        continue
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

    def _get_last_30_minutes_data(self):
        """Pobiera dane z ostatnich 30 minut lub WSZYSTKIE dane jeśli mniej niż 30 minut"""
        if not self.historical_data:
            return []

        # Jeśli mamy mniej niż 30 minut danych, użyj wszystkich
        if len(self.historical_data) < 5:  # Jeśli bardzo mało danych
            return list(self.historical_data)

        # Spróbuj obliczyć rzeczywisty czas
        try:
            # Znajdź najnowszy timestamp
            latest_time = None
            for data in self.historical_data:
                if data.timestamp:
                    latest_time = data.timestamp
                    break

            if latest_time:
                cutoff_time = latest_time - timedelta(minutes=30)
                last_30_data = []

                for data in self.historical_data:
                    try:
                        if data.timestamp and data.timestamp >= cutoff_time:
                            last_30_data.append(data)
                    except Exception:
                        continue

                return last_30_data
        except Exception:
            pass

        # Fallback: weź ostatnie 30 punktów lub wszystkie jeśli mniej
        return list(self.historical_data)[-30:]

    def _get_last_30_minutes_dose_stats(self):
        """Oblicza statystyki dla ostatnich 30 minut lub wszystkich danych"""
        last_30_data = self._get_last_30_minutes_data()

        if not last_30_data:
            return None

        doses = []
        for data in last_30_data:
            try:
                dose = safe_float(data.average_dose)
                doses.append(dose)
            except Exception:
                continue

        if not doses:
            return None

        return {
            'min': min(doses),
            'max': max(doses),
            'avg': sum(doses) / len(doses),
            'count': len(doses),
            'points': len(last_30_data)
        }

    def export_pdf_report(self):
        """Eksportuje raport PDF z danymi z ostatnich 30 minut (lub wszystkich jeśli mniej)"""
        try:
            # Sprawdź czy reportlab jest zainstalowany
            try:
                from reportlab.lib.pagesizes import A4
                from reportlab.pdfgen import canvas
                from reportlab.lib.units import cm, mm
                from reportlab.lib.colors import HexColor
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                from reportlab.lib.styles import getSampleStyleSheet
                from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
                from reportlab.lib import colors
            except ImportError:
                messagebox.showwarning("Uwaga",
                                       "Biblioteka reportlab nie jest zainstalowana.\nZainstaluj: pip install reportlab")
                return

            if not self.historical_data:
                messagebox.showinfo("Info", "Brak danych do raportu")
                return

            # Pobierz dane (ostatnie 30 minut lub wszystkie)
            last_30_data = self._get_last_30_minutes_data()
            dose_stats = self._get_last_30_minutes_dose_stats()

            if not last_30_data or not dose_stats:
                messagebox.showinfo("Info", "Brak danych do wygenerowania raportu")
                return

            # Generuj nazwę pliku
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pdf_filename = os.path.join(self.LOG_DIR, f"geiger_raport_{timestamp}.pdf")

            # Tworzenie PDF
            c = canvas.Canvas(pdf_filename, pagesize=A4)
            width, height = A4

            # Nagłówek
            c.setFont("Helvetica-Bold", 16)
            c.drawString(2 * cm, height - 2 * cm, "RAPORT POMIARÓW PROMIENIOWANIA")

            c.setFont("Helvetica", 10)
            c.drawString(2 * cm, height - 2.5 * cm,
                         f"DRONE GPS GEIGER - Wer. 2.9 | Wygenerowano: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # Określ zakres czasowy
            if len(last_30_data) < 30:  # Jeśli mniej niż 30 punktów
                time_range_text = f"Zakres czasowy: Wszystkie dostępne dane | Liczba punktów: {len(last_30_data)}"
            else:
                time_range_text = f"Zakres czasowy: Ostatnie 30 minut | Liczba punktów: {len(last_30_data)}"

            c.drawString(2 * cm, height - 3 * cm, time_range_text)

            # Linia oddzielająca
            c.line(2 * cm, height - 3.5 * cm, width - 2 * cm, height - 3.5 * cm)

            # Sekcja 1: Statystyki
            y_pos = height - 4.5 * cm
            c.setFont("Helvetica-Bold", 12)
            c.drawString(2 * cm, y_pos, "STATYSTYKI:")
            y_pos -= 0.7 * cm

            c.setFont("Helvetica", 10)
            stats_text = [
                f"Minimalna dawka: {dose_stats['min']:.3f} μSv/h",
                f"Maksymalna dawka: {dose_stats['max']:.3f} μSv/h",
                f"Średnia dawka: {dose_stats['avg']:.3f} μSv/h",
                f"Liczba próbek: {dose_stats['count']}"
            ]

            for stat in stats_text:
                c.drawString(2 * cm, y_pos, stat)
                y_pos -= 0.6 * cm

            # Przewidywania dawek
            y_pos -= 0.3 * cm
            c.setFont("Helvetica-Bold", 12)
            c.drawString(2 * cm, y_pos, "PRZEWIDYWANE DAWKI DOBOWE:")
            y_pos -= 0.7 * cm

            c.setFont("Helvetica", 10)
            hourly_dose = dose_stats['avg']
            daily_dose = hourly_dose * 24
            hourly_mr = hourly_dose * 0.1
            daily_mr = hourly_mr * 24

            predictions = [
                f"Średnia godzinowa: {hourly_dose:.3f} μSv/h ({hourly_mr:.3f} mR/h)",
                f"Przewidywana dobowa: {daily_dose:.3f} μSv ({daily_mr:.3f} mR)"
            ]

            for pred in predictions:
                c.drawString(2 * cm, y_pos, pred)
                y_pos -= 0.6 * cm

            # Tabela punktów pomiarowych
            y_pos -= 0.5 * cm
            c.setFont("Helvetica-Bold", 12)
            c.drawString(2 * cm, y_pos, "TABELA PUNKTÓW POMIAROWYCH:")
            y_pos -= 0.7 * cm

            # Przygotuj dane do tabeli (ogranicz do 20 punktów dla czytelności)
            table_data = [['LP', 'Czas', 'Szerokość', 'Długość', 'Dawka [μSv/h]', 'Poziom']]
            points_to_show = last_30_data[-20:] if len(last_30_data) > 20 else last_30_data

            for i, point in enumerate(points_to_show, 1):
                try:
                    lat = safe_float(point.latitude)
                    lon = safe_float(point.longitude)
                    dose = safe_float(point.average_dose)
                    level_name, _, _ = self.classify_dose(dose)

                    table_data.append([
                        str(i),
                        point.time,
                        f"{lat:.6f}",
                        f"{lon:.6f}",
                        f"{dose:.3f}",
                        level_name.upper()
                    ])
                except Exception:
                    continue

            # Stwórz tabelę
            col_widths = [1 * cm, 2.5 * cm, 3 * cm, 3 * cm, 2.5 * cm, 2 * cm]
            table = Table(table_data, colWidths=col_widths)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4F81BD')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
            ]))

            # Narysuj tabelę
            table_height = len(table_data) * 0.6 * cm
            table.wrapOn(c, width - 4 * cm, height)
            table.drawOn(c, 2 * cm, y_pos - table_height)

            # Legenda
            legend_y = y_pos - table_height - 2 * cm
            c.setFont("Helvetica-Bold", 12)
            c.drawString(2 * cm, legend_y, "LEGENDA POZIOMÓW DAWKI:")
            legend_y -= 0.6 * cm

            c.setFont("Helvetica", 9)
            legend_items = [
                ("● ZIELONY (< 0.10 μSv/h): Norma", HexColor('#008000')),
                ("● ŻÓŁTY (0.10-0.25 μSv/h): Podwyższony", HexColor('#FFFF00')),
                ("● POMARAŃCZOWY (0.25-1.0 μSv/h): Ostrzeżenie", HexColor('#FFA500')),
                ("● CZERWONY (> 1.0 μSv/h): Alarm", HexColor('#FF0000'))
            ]

            for text, color in legend_items:
                c.setFillColor(color)
                c.circle(2 * cm + 0.1 * cm, legend_y - 0.2 * cm, 0.15 * cm, fill=1)
                c.setFillColor(colors.black)
                c.drawString(2 * cm + 0.5 * cm, legend_y - 0.25 * cm, text)
                legend_y -= 0.5 * cm

            # Informacja o cache
            if self.CACHE_ENABLED:
                cache_info_y = legend_y - 0.5 * cm
                c.setFont("Helvetica-Oblique", 8)
                c.drawString(2 * cm, cache_info_y, "Uwaga: Cache mapy jest włączony - szybsze ładowanie map.")

            # Stopka
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(2 * cm, 1 * cm, f"Wygenerowano przez DRONE GPS GEIGER v2.9")
            c.drawString(width - 5 * cm, 1 * cm, f"Strona 1/1")

            # Zapisz PDF
            c.save()

            self.log_message(f"Raport PDF wygenerowany: {pdf_filename}")
            messagebox.showinfo("Sukces", f"Raport PDF wygenerowany:\n{pdf_filename}")

            # Otwórz folder z raportem
            try:
                if sys.platform.startswith("win"):
                    os.startfile(self.LOG_DIR)
                elif sys.platform.startswith("darwin"):
                    os.system(f"open {self.LOG_DIR}")
                else:
                    os.system(f"xdg-open {self.LOG_DIR}")
            except Exception as e:
                self.log_message(f"Błąd otwierania folderu: {e}")

        except Exception as e:
            self.log_message(f"Błąd generowania raportu PDF: {e}")
            import traceback
            traceback.print_exc()
            messagebox.showerror("Błąd", f"Nie udało się wygenerować raportu PDF: {e}")

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
            lines = int(self.log_text.index('end-1c').split('.')[0])
            if lines > 1000:
                self.log_text.delete("1.0", f"{lines - 800}.0")
        except Exception:
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

    # ---------- zamykanie ----------
    def on_closing(self):
        # Ustaw flagę zamykania
        self._is_closing = True

        try:
            # Anulowanie joba dla kolejki
            if self._process_queue_job:
                try:
                    self.root.after_cancel(self._process_queue_job)
                except Exception:
                    pass
            # Anulowanie joba dla tymczasowego markera
            if self.temp_marker_job:
                try:
                    self.root.after_cancel(self.temp_marker_job)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self.disconnect_serial()
        except Exception:
            pass

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