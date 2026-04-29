#!/usr/bin/env python3
"""
HyperspectRus — Hyperspectral Camera App
Raspberry Pi Zero + Picamera2 + STM32 + 800x480 touchscreen
"""

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING FLAG  ←  установите False чтобы полностью отключить запись логов
# ═══════════════════════════════════════════════════════════════════════════════
ENABLE_LOGGING = True

import tkinter as tk
from tkinter import font as tkfont
import threading
import time
import io
import os
import tempfile
import sys
import json
import socket
import struct
import datetime
from pathlib import Path

if os.environ.get('DISPLAY','') == '':
    print('no display found. Using :0.0')
    os.environ.__setitem__('DISPLAY', ':0.0')

# ── PIL ──────────────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageTk, ImageDraw
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("WARNING: Pillow not installed")

# ── GPIO ─────────────────────────────────────────────────────────────────────
try:
    from gpiozero import Button as GpioButton
    _gpio_btn = GpioButton(26, pull_up=True, bounce_time=0.05)
    GPIO_OK = True
except Exception as e:
    GPIO_OK = False
    _gpio_btn = None
    print(f"GPIO not available: {e}")

# ── Camera ───────────────────────────────────────────────────────────────────
try:
    from picamera2 import Picamera2
    from libcamera import controls as libcontrols
    CAM_OK = True
except Exception as e:
    CAM_OK = False
    Picamera2 = None
    print(f"Camera not available: {e}")

# ── STM32 API ────────────────────────────────────────────────────────────────
try:
    import serial
    import serial.tools.list_ports
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False
    print("WARNING: pyserial not installed")

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

W, H         = 800, 480
ASSETS       = Path("assets")
LOG_DIR      = Path("logs")           # папка рядом со скриптом
SESSIONS_DIR = Path("sessions")
PENDING_STATE_FILE = SESSIONS_DIR / ".pending_state.json"
PREVIEW_W    = 490

# ═══════════════════════════════════════════════════════════════════════════════
# BATTERY  ←  настройте под вашу батарею
# ═══════════════════════════════════════════════════════════════════════════════
# Напряжение батареи в милливольтах:
#   BATTERY_MV_EMPTY — соответствует 0% (устройство может выключиться в любой момент)
#   BATTERY_MV_FULL  — соответствует 100%
# Линейная интерполяция между ними. Значения подбираются под конкретный аккумулятор.
BATTERY_MV_EMPTY = 3400
BATTERY_MV_FULL  = 4100

# Порог в процентах, при котором показываем предупреждение о низком заряде.
# Окно появится повторно через LOW_BATTERY_REMIND_S секунд после закрытия.
LOW_BATTERY_PCT_THRESHOLD = 0
LOW_BATTERY_REMIND_S      = 60
PANEL_X      = PREVIEW_W + 6
PANEL_W      = W - PREVIEW_W - 10
STATUS_H     = 34
PREVIEW_FPS  = 25

# ── Camera settings ──────────────────────────────────────────────────────────
PREVIEW_EXPOSURE_US  = 15000
CAPTURE_EXPOSURE_US  = 1000

# ── Format ───────────────────────────────────────────────────────────────────
CAPTURE_FORMAT = "raw"   # "jpeg" or "raw" — change here

# ── LED table: (led_index, wavelength_nm, capture_duty%) ─────────────────────
LED_TABLE = [
    (1, 450, 100),
    (2, 517, 100),
    (3, 671,  60),
    (4, 775,  60),
    (5, 803,  50),
    (6, 851,  40),
    (7, 888,  60),
    (8, 939, 100),
]

# ── LED modes ────────────────────────────────────────────────────────────────
M_RGB = "rgb"
M_IR  = "ir"
M_NBI = "nbi"

PREVIEW_LED_DUTY = (5, 10, 10, 10, 10, 10, 5, 5)

# ── Network ──────────────────────────────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5555

# ── Palette ──────────────────────────────────────────────────────────────────
BG         = "#000000"
STATUS_BG  = "#0A0A0A"
CARD_BG    = "#D4D4D4"
CARD_FG    = "#1A1A1A"
BTN_IDLE   = "#CECECE"
BTN_ACTIVE = "#6E9B1E"
BTN_FINISH = "#767676"
BTN_DANGER = "#B03030"
BTN_OK     = "#4A8A18"
TEXT_WHITE = "#FFFFFF"
TEXT_DIM   = "#888888"
ACCENT_GRN = "#6FCF3A"
WARN_COL   = "#FF6600"


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGER
# ═══════════════════════════════════════════════════════════════════════════════
#
# Формат строки:
#   2024-01-15 14:23:05.123 | STARTUP       | 3842 mV  91% | App started
#
# Файл: logs/hyperspectrus_YYYY-MM.log  (новый файл каждый месяц)
# Запись: append + flush + fsync после каждой строки — безопасно при жёстком
#         отключении питания. fsync даёт ~5-10 мс задержки, но вызывается
#         только на событиях (не в цикле), поэтому на UI не влияет.
#
# Оценка объёма за месяц (30 дней):
#   Типичная рабочая смена — 20 сессий по 8 снимков каждая:
#     • STARTUP       — 1 строка/запуск,  ~2 запуска/день   =   60 строк
#     • TASK_RECV     — 1 строка/сессия,  20 сессий/день    =  600 строк
#     • LED_ON/OFF    — 2 строки × 8 LED × 20 сессий        = 3200 строк
#     • PHOTO         — 8 строк/сессия,   20 сессий          = 4800 строк  (8×20×30=4800/мес)
#     • FILE_SAVED    — 16 стр/сессия     20 сессий          = 9600 строк
#     • SEND_START/OK — 2 строки/сессия                     =  600 строк
#     • CHARGE_*      — ~4 события/день                      =  120 строк
#     • PC_CONN/DISC  — ~4 события/день                      =  120 строк
#   Итого ≈ 19 100 строк/месяц × ~95 байт = ~1.8 МБ/месяц
#   При пиковой нагрузке (40 сессий/день) ≈ 3.5 МБ/месяц
# ═══════════════════════════════════════════════════════════════════════════════

class Logger:
    """Потокобезопасный логгер с посекундными метками и немедленным fsync."""

    # Ширина колонки EVENT для выравнивания
    _EV_WIDTH = 13

    # Уровни / типы событий
    STARTUP      = "STARTUP"
    SHUTDOWN     = "SHUTDOWN"
    TASK_RECV    = "TASK_RECV"
    LED_ON       = "LED_ON"
    LED_OFF      = "LED_OFF"
    PHOTO        = "PHOTO"
    FILE_SAVED   = "FILE_SAVED"
    SEND_START   = "SEND_START"
    SEND_FILE    = "SEND_FILE"
    SEND_OK      = "SEND_OK"
    SEND_FAIL    = "SEND_FAIL"
    CHARGE_ON    = "CHARGE_ON"
    CHARGE_OFF   = "CHARGE_OFF"
    PC_CONN      = "PC_CONN"
    PC_DISC      = "PC_DISC"
    ERROR        = "ERROR"

    def __init__(self, log_dir: Path):
        self._dir   = log_dir
        self._lock  = threading.Lock()
        self._fh    = None          # текущий файловый дескриптор
        self._month = None          # "YYYY-MM" открытого файла
        if ENABLE_LOGGING:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"Logger: cannot create log dir: {e}")

    # ── public API ───────────────────────────────────────────────────────────

    def log(self, event: str, detail: str = "", voltage_mv: int = -1):
        """Записать одну строку в лог.

        Args:
            event:      тип события (константа класса)
            detail:     произвольное описание
            voltage_mv: напряжение батареи в мВ (-1 = неизвестно)
        """
        if not ENABLE_LOGGING:
            return
        ts      = datetime.datetime.now()
        ts_str  = ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
        bat_str = self._fmt_bat(voltage_mv)
        ev_str  = event.ljust(self._EV_WIDTH)
        line    = f"{ts_str} | {ev_str} | {bat_str} | {detail}\n"
        with self._lock:
            try:
                fh = self._get_fh(ts)
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())   # гарантия записи на SD даже при жёстком отключении
            except Exception as e:
                print(f"Logger write error: {e}")
                self._fh = None         # попробуем переоткрыть при следующем вызове

    def close(self):
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    # ── private ──────────────────────────────────────────────────────────────

    def _get_fh(self, ts: datetime.datetime):
        """Вернуть (или открыть/ротировать) файловый дескриптор."""
        month = ts.strftime("%Y-%m")
        if self._fh is None or month != self._month:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
            path = self._dir / f"hyperspectrus_{month}.log"
            self._fh    = open(path, "a", encoding="utf-8", buffering=1)
            self._month = month
            # Записываем разделитель при открытии нового/существующего файла
            sep = "-" * 80 + "\n"
            self._fh.write(sep)
            self._fh.flush()
        return self._fh

    @staticmethod
    def _fmt_bat(mv: int) -> str:
        if mv <= 0:
            return "  ??? mV  ??%"
        rng = max(1, BATTERY_MV_FULL - BATTERY_MV_EMPTY)
        pct = max(0, min(100, int((mv - BATTERY_MV_EMPTY) / rng * 100)))
        return f"{mv:5d} mV {pct:3d}%"


# Глобальный экземпляр; инициализируется в App.__init__
_logger: "Logger | None" = None

def log(event: str, detail: str = "", voltage_mv: int = -1):
    """Удобная глобальная функция-обёртка."""
    if _logger:
        _logger.log(event, detail, voltage_mv)


# ═══════════════════════════════════════════════════════════════════════════════
# STM32 CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════════

class STM32:
    PORTS   = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"]
    BAUD    = 9600
    TIMEOUT = 0.1

    def __init__(self):
        self.ser       = None
        self.connected = False
        self._lock     = threading.Lock()
        self._connect()

    def _connect(self):
        if not SERIAL_OK:
            return
        for port in self.PORTS:
            try:
                s = serial.Serial(port, self.BAUD, timeout=self.TIMEOUT)
                self.ser       = s
                self.connected = True
                time.sleep(0.3)
                print(f"STM32 connected on {port}")
                return
            except Exception:
                pass
        try:
            for p in serial.tools.list_ports.comports():
                try:
                    s = serial.Serial(p.device, self.BAUD, timeout=self.TIMEOUT)
                    self.ser       = s
                    self.connected = True
                    time.sleep(0.3)
                    print(f"STM32 connected on {p.device}")
                    return
                except Exception:
                    pass
        except Exception:
            pass
        print("STM32 not found")

    def _cmd(self, cmd: str) -> str:
        with self._lock:
            if not self.connected or not self.ser:
                return ""
            try:
                self.ser.reset_input_buffer()
                self.ser.write(cmd.encode("utf-8"))
                self.ser.flush()
                buf = b""
                buf = self.ser.read_until(b"\n", size=256)
                return buf.decode("utf-8", errors="ignore").strip()
            except serial.SerialException as e:
                print(f"STM32 serial error: {e}")
                self.connected = False
                return ""
            except Exception as e:
                print(f"STM32 error: {e}")
                return ""

    def reconnect(self):
        with self._lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser       = None
            self.connected = False
        time.sleep(1)
        self._connect()

    def get_charging_state(self) -> int:
        r = self._cmd("getChargingState\n")
        try:
            return int(r)
        except Exception:
            return -1

    def get_battery_mv(self) -> int:
        r = self._cmd("getBatteryVoltage\n")
        try:
            return int(r)
        except Exception:
            return -1

    def led_duty(self, led: int, duty: int):
        self._cmd(f"setLedDuty{led}{duty}\n")

    def led_on(self, led: int):
        self._cmd(f"setOne{led}\n")

    def led_off(self, led: int):
        self._cmd(f"setStopOne{led}\n")

    def all_off(self):
        for i in range(1, 9):
            self.led_off(i)

    def set_preview_leds(self, mode: str, duty: int = PREVIEW_LED_DUTY):
        self.all_off()
        if mode == M_RGB:
            for led in [1, 2, 3]:
                self.led_duty(led, duty[led-1])
                self.led_on(led)
        elif mode == M_IR:
            for led in [7, 8]:
                self.led_duty(led, duty[led-1])
                self.led_on(led)
        elif mode == M_NBI:
            for led in [1, 2]:
                self.led_duty(led, duty[led-1])
                self.led_on(led)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "—"

def mv_to_pct(mv: int) -> int:
    if mv <= 0:
        return 0
    rng = max(1, BATTERY_MV_FULL - BATTERY_MV_EMPTY)
    pct = int((mv - BATTERY_MV_EMPTY) / rng * 100)
    return max(0, min(100, pct))


def load_asset(name: str, size=None):
    if not PIL_OK:
        return None
    path = ASSETS / name
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        if size:
            img = img.resize(size, Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"Asset load error {name}: {e}")
        return None


def battery_icon_name(pct: int, charging: int) -> str:
    thresholds = [0, 25, 50, 75, 100]
    idx = min(range(len(thresholds)), key=lambda i: abs(thresholds[i] - pct))
    offset = 5 if charging in (1, 2) else 0
    return f"bat{idx + 1 + offset}.jpg"


def resize_to_fill(img: Image.Image, tw: int, th: int) -> Image.Image:
    scale = th / img.height
    new_w = int(img.width * scale)
    resized = img.resize((new_w, th), Image.LANCZOS)
    if new_w > tw:
        left = (new_w - tw) // 2
        resized = resized.crop((left, 0, left + tw, th))
    return resized


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORK SERVER (runs in background thread)
# ═══════════════════════════════════════════════════════════════════════════════

class NetworkServer:
    """Listens for PC connections; receives tasks, sends back photos."""

    def __init__(self, app):
        self.app             = app
        self._running        = True
        self._sock           = None
        self._client         = None
        self._ack_event      = threading.Event()   # сигнал об ACK от PC
        self._nack_event     = threading.Event()   # сигнал о NACK от PC
        self._last_ack_file  = None                # имя файла из последнего ACK
        self._last_nack_file = None                # имя файла из последнего NACK
        # ── ВАЖНО: один lock на ВСЕ записи в сокет.
        # sendall() из разных потоков НЕ атомарен — без lock'а pong/ack/header
        # могут вклиниться в середину данных файла → MD5 mismatch у ПК. ──
        self._send_lock      = threading.Lock()
        # Незавершённая сессия для повтора при переподключении
        self._pending_session = None  # (session_dir, patient_id, notes, sent_files)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((SERVER_HOST, SERVER_PORT))
            self._sock.listen(1)
            self._sock.settimeout(1.0)
            print(f"Network server listening on {SERVER_HOST}:{SERVER_PORT}")
        except Exception as e:
            print(f"Server bind error: {e}")
            return

        while self._running:
            try:
                conn, addr = self._sock.accept()
                # ── Агрессивный keepalive на принятом сокете ──────────────────
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for opt_name, val in (("TCP_KEEPIDLE", 5),
                                      ("TCP_KEEPINTVL", 2),
                                      ("TCP_KEEPCNT", 3)):
                    opt = getattr(socket, opt_name, None)
                    if opt is not None:
                        try:
                            conn.setsockopt(socket.IPPROTO_TCP, opt, val)
                        except OSError:
                            pass
                self._client = conn
                print(f"PC connected from {addr}")
                self.app.root.after(0, lambda: self.app.set_pc_connected(True))
                # Если есть незавершённая сессия — предложить отправить её повторно
                if self._pending_session:
                    self.app.root.after(
                        0, lambda: self.app._offer_retry_pending_session()
                    )
                # Запускаем приём в этом же потоке. _handle вернётся при разрыве.
                self._handle(conn)
                # ── ВАЖНО: force shutdown+close — это аборт любого in-flight
                #    sendall() в потоке send_photos. Без этого send_photos
                #    висел бы в sendall() на мёртвом сокете до OS-level keepalive. ──
                try: conn.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: conn.close()
                except Exception: pass
                self._client = None
                self.app.root.after(0, lambda: self.app.set_pc_connected(False))
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Server accept error: {e}")
                time.sleep(1)

    # Сколько секунд тишины от ПК = соединение мёртвое.
    HEARTBEAT_TIMEOUT_S = 10.0

    def _handle(self, conn: socket.socket):
        """Цикл приёма от ПК. Выходит при разрыве или heartbeat timeout."""
        buf = b""
        conn.settimeout(2.0)
        last_recv = time.monotonic()
        try:
            while self._running:
                # Heartbeat: если ПК ничего не шлёт N секунд — считаем его мёртвым.
                # ПК шлёт ping каждые 3 сек, так что 10 сек тишины = реальная проблема.
                if time.monotonic() - last_recv > self.HEARTBEAT_TIMEOUT_S:
                    print(f"PC heartbeat timeout ({self.HEARTBEAT_TIMEOUT_S}s) — closing")
                    break
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except (OSError, ConnectionError) as e:
                    print(f"recv error: {e}")
                    break
                if not chunk:
                    break
                last_recv = time.monotonic()
                buf += chunk
                # Messages delimited by newline JSON
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                        self._on_message(conn, msg)
                    except Exception as e:
                        print(f"Protocol error: {e}")
        except Exception as e:
            print(f"Client handler error: {e}")

    def _on_message(self, conn, msg: dict):
        cmd = msg.get("cmd")
        if cmd == "task":
            patient_id = msg.get("patient_id", "")
            notes      = msg.get("notes", "")
            self.app.root.after(0, lambda: self.app.receive_task(patient_id, notes))
        elif cmd == "ping":
            self._send(conn, {"cmd": "pong"})
        elif cmd == "ack":
            # PC подтвердил приём файла
            self._last_ack_file = msg.get("filename")
            self._ack_event.set()
        elif cmd == "nack":
            # PC сообщил о битом файле (MD5 mismatch и т.п.)
            self._last_nack_file = msg.get("filename")
            self._nack_event.set()
            print(f"NACK from PC: {self._last_nack_file} reason={msg.get('reason')}")
        elif cmd == "cancel_task":
            # PC попросил отменить текущую задачу
            self.app.root.after(0, self.app.cancel_current_task)
            self._send(conn, {"cmd": "task_cancelled"})

    def _send(self, conn, data: dict) -> bool:
        """Отправить JSON-строку. Использует send_lock — безопасно из любого потока."""
        try:
            line = json.dumps(data).encode("utf-8") + b"\n"
            with self._send_lock:
                conn.sendall(line)
            return True
        except Exception as e:
            print(f"Send error: {e}")
            return False

    def _send_raw(self, conn, payload: bytes) -> bool:
        """Отправить байты под lock'ом. Используется для больших данных файлов."""
        try:
            with self._send_lock:
                conn.sendall(payload)
            return True
        except Exception as e:
            print(f"Send raw error: {e}")
            return False

    def _wait_ack_or_nack(self, expected_filename: str, timeout: float = 12.0) -> str:
        """Ждём ACK или NACK от PC на конкретный файл.
        Возвращает 'ack', 'nack' или 'timeout'."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._last_ack_file == expected_filename:
                return "ack"
            if self._last_nack_file == expected_filename:
                return "nack"
            # Спим до следующего события или 1 с
            self._ack_event.clear()
            self._nack_event.clear()
            remaining = deadline - time.monotonic()
            # Любой из event'ов разбудит wait
            if self._ack_event.wait(timeout=min(remaining, 1.0)):
                pass
            if self._last_ack_file == expected_filename:
                return "ack"
            if self._last_nack_file == expected_filename:
                return "nack"
        return "timeout"

    def send_photos(self, session_dir: Path, patient_id: str, notes: str,
                    progress_cb=None, skip_files: set = None):
        """Send all photos from session_dir to connected PC using relative paths.

        skip_files — набор rel_path строк уже отправленных файлов (для повтора).
        """
        conn = self._client
        if not conn:
            print("No PC connected — cannot send photos")
            return False

        skip_files = skip_files or set()
        sent_files = set(skip_files)

        try:
            # Ждём завершения фоновых потоков записи
            time.sleep(0.5)

            all_files = sorted(f for f in session_dir.rglob("*") if f.is_file())
            files = [f for f in all_files
                     if f.relative_to(session_dir).as_posix() not in skip_files]
            total = len(files)

            meta = {
                "cmd":        "session_start",
                "session_id": session_dir.name,
                "patient_id": patient_id,
                "notes":      notes,
                "file_count": total,
                "resume":     len(skip_files) > 0,
            }
            if not self._send(conn, meta):
                raise ConnectionError("session_start send failed")

            import hashlib

            for idx, f in enumerate(files, start=1):
                if self._client is None or self._client is not conn:
                    raise ConnectionError("client socket replaced")

                rel_path = f.relative_to(session_dir).as_posix()
                data     = f.read_bytes()
                checksum = hashlib.md5(data).hexdigest()

                if progress_cb:
                    progress_cb(idx, total, rel_path)

                # До 3-х попыток отправки одного файла (на случай NACK)
                attempt = 0
                while attempt < 3:
                    attempt += 1

                    header = json.dumps({
                        "cmd":      "file",
                        "filename": rel_path,
                        "size":     len(data),
                        "md5":      checksum,
                    }).encode("utf-8") + b"\n"

                    self._last_ack_file  = None
                    self._last_nack_file = None
                    self._ack_event.clear()
                    self._nack_event.clear()

                    # ── КЛЮЧЕВОЙ ФИКС: header И data отправляются под одним lock'ом.
                    # Без этого pong из recv-потока может вклиниться между ними или
                    # внутрь данных файла → ПК увидит битый MD5. ──
                    payload = header + data
                    if not self._send_raw(conn, payload):
                        raise ConnectionError("file send failed")

                    result = self._wait_ack_or_nack(rel_path, timeout=12.0)
                    if result == "ack":
                        sent_files.add(rel_path)
                        break
                    elif result == "nack":
                        print(f"NACK on {rel_path}, retrying ({attempt}/3)…")
                        continue
                    else:  # timeout
                        self._pending_session = (
                            session_dir, patient_id, notes, sent_files
                        )
                        print(f"Transfer interrupted after {len(sent_files)} files")
                        self._maybe_trigger_retry()
                        return False

                if rel_path not in sent_files:
                    # Все 3 попытки получили NACK — проблема не сетевая
                    self._pending_session = (
                        session_dir, patient_id, notes, sent_files
                    )
                    print(f"3 NACKs in a row on {rel_path}, giving up")
                    self._maybe_trigger_retry()
                    return False

            self._send(conn, {"cmd": "session_end"})
            self._pending_session = None
            return True

        except Exception as e:
            print(f"Photo send error: {e}")
            self._pending_session = (
                session_dir, patient_id, notes, sent_files
            )
            self._maybe_trigger_retry()
            return False

    def _maybe_trigger_retry(self):
        """Если PC уже переподключился — запланировать повтор."""
        if self._client is not None:
            self.app.root.after(800, self.app._offer_retry_pending_session)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

class App:

    def __init__(self):
        # ── State ─────────────────────────────────────────────────────────────
        self.screen       = None
        self.led_mode     = M_RGB
        self.battery_pct  = 0
        self.charging     = 0
        self.stm_ok       = False
        self.cam_running  = False
        self.captures     = []       # list of (wl, PIL.Image)
        self.prev_idx     = 0
        self.patient_id   = "—"
        self.notes        = ""
        self.session_dir  = None
        # Low-battery popup state
        self._low_bat_window         = None     # активный Toplevel или None
        self._low_bat_next_show_ts   = 0.0      # когда можно показать снова
        self.saved_sets   = []       # list of session dirs to send
        self.pc_connected = False
        self._preview_job = None

        # ── Logger ────────────────────────────────────────────────────────────
        global _logger
        _logger = Logger(LOG_DIR)

        # ── Hardware ──────────────────────────────────────────────────────────
        self.stm    = STM32()
        self.stm_ok = self.stm.connected
        self.cam    = None
        self._init_camera()

        # ── Root window ───────────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("HyperspectRus")
        self.root.geometry(f"{W}x{H}+0+0")
        self.root.configure(bg=BG)
        self.root.attributes("-fullscreen", True)
        self.root.resizable(False, False)
        self.root.option_add("*tearOff", False)
        self.root.config(cursor="none")

        # ── Fonts ─────────────────────────────────────────────────────────────
        self.fnt_xl  = tkfont.Font(family="DejaVu Sans", size=40, weight="bold")
        self.fnt_lg  = tkfont.Font(family="DejaVu Sans", size=28, weight="bold")
        self.fnt_md  = tkfont.Font(family="DejaVu Sans", size=20)
        self.fnt_mdb = tkfont.Font(family="DejaVu Sans", size=20, weight="bold")
        self.fnt_sm  = tkfont.Font(family="DejaVu Sans", size=13)
        self.fnt_smb = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")

        self._photo_cache = {}

        # ── Build all screens ─────────────────────────────────────────────────
        self.frames = {}
        self._build_splash()
        self._build_waiting()
        self._build_task_select()
        self._build_main()
        self._build_finish_confirm()
        self._build_sending()
        self._build_capturing()
        self._build_photo_preview()

        # ── GPIO shutter ──────────────────────────────────────────────────────
        if GPIO_OK and _gpio_btn:
            _gpio_btn.when_pressed = lambda: self.root.after(0, self._shutter_pressed)

        # ── Background threads ────────────────────────────────────────────────
        threading.Thread(target=self._battery_thread, daemon=True).start()
        self.net_server = NetworkServer(self)

        # ── Boot ──────────────────────────────────────────────────────────────
        log(Logger.STARTUP,
            f"App started | format={CAPTURE_FORMAT} | cam={'OK' if CAM_OK else 'NO'}"
            f" | stm={'OK' if self.stm_ok else 'NO'}",
            voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        self.root.after(100, self._boot)
        self.root.mainloop()

    # ─────────────────────────────────────────────────────────────────────────
    # CAMERA
    # ─────────────────────────────────────────────────────────────────────────

    def _init_camera(self):
        if not CAM_OK:
            return
        try:
            self.cam = Picamera2()

            # Preview config: exact display size so no crop is needed
            self._cfg_jpeg = self.cam.create_video_configuration(
                main={"size": (PREVIEW_W, H-STATUS_H), "format": "RGB888"},
            )

            # Capture config: full-res main + raw sensor stream
            self._cfg_raw = self.cam.create_video_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
                raw={},          # picamera2 picks sensor native format
            )

            # Start with JPEG config
            self.cam.configure(self._cfg_jpeg)
            self.cam.set_controls({
                "AeEnable":    False,
                "AwbEnable":   False,
                "ExposureTime": CAPTURE_EXPOSURE_US,
                "ColourGains": (1.0, 1.0),
                "AfMode":      libcontrols.AfModeEnum.Manual,
                "LensPosition": 12.0,
            })
            print(f"Camera initialized")
        except Exception as e:
            print(f"Camera init error: {e}")
            self.cam = None

    def _apply_preview_settings(self):
        if self.cam:
            self.cam.set_controls({"ExposureTime": PREVIEW_EXPOSURE_US})

    def _apply_capture_settings(self):
        if self.cam:
            self.cam.set_controls({"ExposureTime": CAPTURE_EXPOSURE_US})

    def _start_cam_preview(self):
        if not self.cam or self.cam_running:
            return
        try:
            self.cam.start()
            self.cam_running = True
            self._apply_preview_settings()
            self._preview_job = self.root.after(40, self._do_preview_frame)
        except Exception as e:
            print(f"Camera start error: {e}")

    def _stop_cam_preview(self):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
            self._preview_job = None
        if self.cam and self.cam_running:
            try:
                self.cam.stop()
            except Exception:
                pass
            self.cam_running = False

    def _do_preview_frame(self):
        if self.screen != "main" or not self.cam_running:
            return
        try:
            arr = self.cam.capture_array()
            ph  = ImageTk.PhotoImage(Image.fromarray(arr))
            self.main_prev_lbl.config(image=ph, text="")
            self.main_prev_lbl.image = ph
            self._photo_cache["preview"] = ph
        except Exception:
            pass
        delay = max(1, int(1000 / PREVIEW_FPS))
        self._preview_job = self.root.after(delay, self._do_preview_frame)

    # ─────────────────────────────────────────────────────────────────────────
    # BATTERY THREAD
    # ─────────────────────────────────────────────────────────────────────────

    def _battery_thread(self):
        while True:
            try:
                if not self.stm.connected:
                    self.stm.reconnect()
                    self.stm_ok = self.stm.connected
                    self.root.after(0, self._refresh_warns)

                if self.stm.connected:
                    mv  = self.stm.get_battery_mv()
                    chg = self.stm.get_charging_state()
                    pct = mv_to_pct(mv) if mv > 0 else self.battery_pct
                    old_chg = self.charging

                    self.battery_pct = pct
                    if chg >= 0:
                        self.charging = chg

                    self.root.after(0, self._refresh_battery)
                    self.root.after(0, self._check_low_battery)

                    if chg >= 0 and chg != old_chg:
                        self.root.after(0, self._on_charge_changed)

            except Exception as e:
                print(f"Battery thread error: {e}")
            time.sleep(5)

    def _on_charge_changed(self):
        if self.charging in (1, 2):
            log(Logger.CHARGE_ON,
                f"charging state={self.charging}",
                voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
            # На зарядке — закрываем предупреждение о низком заряде
            if self._low_bat_window is not None:
                try:
                    self._low_bat_window.destroy()
                except Exception:
                    pass
                self._low_bat_window = None
                self._low_bat_next_show_ts = 0.0
            if self.screen != "splash":
                self._stop_cam_preview()
                threading.Thread(target=self.stm.all_off, daemon=True).start()
                self._show_splash()
        else:
            log(Logger.CHARGE_OFF,
                f"charging state={self.charging}",
                voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
            if self.screen == "splash":
                self._show_waiting()

    def _refresh_battery(self):
        # Status bar battery icon
        icon = battery_icon_name(self.battery_pct, self.charging)
        ph   = load_asset(icon, size=(72, 35))
        lbl  = getattr(self, "main_bat_lbl", None)
        if lbl:
            if ph:
                self._photo_cache["bat_sm"] = ph
                lbl.config(image=ph, text="")
                lbl.image = ph
            else:
                sym = "⚡" if self.charging else "🔋"
                lbl.config(text=f"{sym} {self.battery_pct}%", image="")

        if self.screen == "splash":
            self._refresh_splash_bat()

    # ── Low battery popup ────────────────────────────────────────────────────
    def _check_low_battery(self):
        """Вызывается из батарейного потока через root.after.
        Показывает окно если батарея ≤ порога, не на зарядке, и прошёл cooldown.
        Окно не модальное: НИКАКИЕ запущенные процессы (съёмка, отправка) не блокируются."""
        if self.charging in (1, 2):
            # На зарядке — никаких предупреждений
            return
        if self.battery_pct > LOW_BATTERY_PCT_THRESHOLD:
            return
        # Окно уже открыто? Не плодим дубликаты
        if self._low_bat_window is not None:
            try:
                if self._low_bat_window.winfo_exists():
                    return
            except Exception:
                pass
            self._low_bat_window = None
        # Cooldown между показами
        if time.monotonic() < self._low_bat_next_show_ts:
            return
        self._show_low_battery_popup()

    def _show_low_battery_popup(self):
        try:
            top = tk.Toplevel(self.root)
            top.title("Низкий заряд")
            top.configure(bg=BG)
            top.overrideredirect(True)   # без рамки ОС — выглядит как наше окно
            # Поверх всех окон, но без grab_set — не блокирует фон
            try:
                top.attributes("-topmost", True)
            except Exception:
                pass
            top.transient(self.root)

            POP_W, POP_H = 560, 240
            x = (W - POP_W) // 2
            y = (H - POP_H) // 2
            top.geometry(f"{POP_W}x{POP_H}+{x}+{y}")

            # Контейнер с рамкой
            border = tk.Frame(top, bg=WARN_COL, bd=0)
            border.place(x=0, y=0, width=POP_W, height=POP_H)
            inner = tk.Frame(border, bg=CARD_BG, bd=0)
            inner.place(x=3, y=3, width=POP_W - 6, height=POP_H - 6)

            tk.Label(inner, text="⚠  Низкий заряд батареи",
                     font=("DejaVu Sans", 22, "bold"),
                     fg=WARN_COL, bg=CARD_BG,
                     ).place(relx=0.5, y=30, anchor="n")

            tk.Label(inner,
                     text=("Заряд критически низкий.\n"
                           "Устройство может выключиться в любой момент.\n"
                           "Поставьте устройство на зарядку."),
                     font=("DejaVu Sans", 14),
                     fg=TEXT_WHITE, bg=CARD_BG, justify="center",
                     ).place(relx=0.5, y=82, anchor="n")

            def close():
                self._low_bat_next_show_ts = time.monotonic() + LOW_BATTERY_REMIND_S
                try: top.destroy()
                except Exception: pass
                self._low_bat_window = None

            tk.Button(inner, text="OK",
                      font=("DejaVu Sans", 16, "bold"),
                      bg=BTN_ACTIVE, fg="#111111",
                      activebackground=BTN_ACTIVE, activeforeground="#111111",
                      relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                      command=close,
                      ).place(relx=0.5, rely=1.0, y=-26, anchor="s",
                              width=180, height=44)

            # Закрытие по WM (если оконный менеджер всё-таки покажет крестик)
            top.protocol("WM_DELETE_WINDOW", close)

            self._low_bat_window = top
        except Exception as e:
            print(f"low battery popup error: {e}")
            self._low_bat_window = None
            # Поставим cooldown даже если окно не показалось,
            # чтобы не спамить попытками каждые 5 сек
            self._low_bat_next_show_ts = time.monotonic() + LOW_BATTERY_REMIND_S

    def _refresh_splash_bat(self):
        icon = battery_icon_name(self.battery_pct, self.charging)
        ph   = load_asset(icon, size=(320, 160))
        lbl  = getattr(self, "splash_bat_lbl", None)
        if not lbl:
            return
        if ph:
            self._photo_cache["bat_lg"] = ph
            lbl.config(image=ph, text="")
            lbl.image = ph
        else:
            sym = "⚡" if self.charging else "🔋"
            lbl.config(text=f"{sym}  {self.battery_pct}%",
                       font=("DejaVu Sans", 32), fg=TEXT_WHITE, image="")

    def _refresh_warns(self):
        warn = "" if self.stm_ok else "⚠  Контроллер отключён"
        for attr in ("splash_warn", "wait_warn", "task_warn", "main_warn", "finish_warn", "cap_warn"):
            lbl = getattr(self, attr, None)
            if lbl:
                lbl.config(text=warn)

    # ─────────────────────────────────────────────────────────────────────────
    # NETWORK CALLBACKS
    # ─────────────────────────────────────────────────────────────────────────

    def set_pc_connected(self, connected: bool):
        self.pc_connected = connected
        if connected:
            log(Logger.PC_CONN, "PC connected",
                voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        else:
            log(Logger.PC_DISC, "PC disconnected",
                voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        if self.screen == "waiting":
            self._update_wait_pc_label()

    def _offer_retry_pending_session(self):
        """Вызывается когда PC переподключился и есть незавершённая сессия."""
        ps = self.net_server._pending_session
        if not ps:
            return
        # Идемпотентность: не запускаем второй retry, если первый ещё работает
        if getattr(self, "_retry_in_progress", False):
            return
        # Проверяем что PC реально подключён прямо сейчас
        if self.net_server._client is None:
            return
        self._retry_in_progress = True
        session_dir, patient_id, notes, sent_files = ps
        print(f"Retrying pending session: {session_dir}, "
              f"already sent: {len(sent_files)} files")

        # Переключаем UI на экран отправки чтобы было видно прогресс
        try:
            total = sum(1 for f in session_dir.rglob("*") if f.is_file())
        except Exception:
            total = 0
        try:
            self._show_sending(total)
        except Exception:
            pass

        def _do_retry():
            ok = False
            try:
                ok = self.net_server.send_photos(
                    session_dir, patient_id, notes,
                    skip_files=sent_files,
                    progress_cb=lambda c, t, fn:
                        self.root.after(0, self._update_send_progress, c, t, fn),
                )
            finally:
                self._retry_in_progress = False
            if ok:
                # Успешная досылка — удаляем state и папку, чистим экран
                self.root.after(0, self._cleanup_after_retry, session_dir)

        threading.Thread(target=_do_retry, daemon=True).start()

    def _cleanup_after_retry(self, session_dir: Path):
        """Вызывается из UI-потока после успешной досылки восстановленной сессии."""
        self._clear_pending_state()
        try:
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)
        except Exception:
            pass
        self._reset_session()
        self.patient_id = "—"
        self.notes      = ""
        if self.screen != "splash":
            self._show_waiting()

    def receive_task(self, patient_id: str, notes: str):
        self.patient_id  = patient_id
        self.notes       = notes
        self.saved_sets  = []
        self.session_dir = None
        log(Logger.TASK_RECV,
            f"patient_id='{patient_id}' notes='{notes[:60]}'",
            voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        # Update labels
        txt = f'ID пациента: "{patient_id}"'
        for attr in ("task_title", "finish_title"):
            lbl = getattr(self, attr, None)
            if lbl:
                lbl.config(text=txt)
        # Always jump to task confirm screen when a task arrives
        self._show_task_select()

    def cancel_current_task(self):
        """Отмена задачи по запросу с ПК. Сбрасываем состояние и идём в waiting."""
        log(Logger.TASK_RECV,
            f"Task CANCELLED by PC | patient='{self.patient_id}'",
            voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        # Удаляем папку сессии если она была создана но ничего не отправлено
        if self.session_dir is not None:
            try:
                import shutil
                shutil.rmtree(self.session_dir, ignore_errors=True)
            except Exception:
                pass
        self._reset_session()
        self.patient_id = "—"
        self.notes      = ""
        self._clear_pending_state()
        # Очистить также pending_session в NetworkServer (если был)
        try:
            self.net_server._pending_session = None
        except Exception:
            pass
        if self.screen != "splash":
            self._stop_cam_preview()
            self._show_waiting()

    # ── Session persistence (восстановление после жёсткого выключения) ────────

    def _save_pending_state(self):
        """Сохраняет состояние сессии в JSON атомарно. Вызывается после каждого
        сохранённого набора и перед началом отправки."""
        if self.session_dir is None:
            return
        state = {
            "patient_id":  self.patient_id,
            "notes":       self.notes,
            "session_dir": str(self.session_dir),
            "saved_sets":  self.saved_sets,
            "ts":          datetime.datetime.now().isoformat(timespec="seconds"),
        }
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = PENDING_STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, PENDING_STATE_FILE)   # атомарно
        except Exception as e:
            print(f"save pending state error: {e}")

    def _clear_pending_state(self):
        try:
            if PENDING_STATE_FILE.exists():
                PENDING_STATE_FILE.unlink()
        except Exception as e:
            print(f"clear pending state error: {e}")

    def _load_pending_state(self) -> dict:
        """Возвращает dict состояния если файл валиден и папка содержит файлы.
        Иначе None и удаляет битый/пустой state."""
        if not PENDING_STATE_FILE.exists():
            return None
        try:
            state = json.loads(PENDING_STATE_FILE.read_text(encoding="utf-8"))
            sd = Path(state["session_dir"])
            if not sd.exists():
                self._clear_pending_state()
                return None
            # Проверим есть ли реально файлы
            files = [f for f in sd.rglob("*") if f.is_file()]
            if not files:
                # Папка пустая — удаляем
                try:
                    import shutil
                    shutil.rmtree(sd, ignore_errors=True)
                except Exception:
                    pass
                self._clear_pending_state()
                return None
            state["file_count"] = len(files)
            return state
        except Exception as e:
            print(f"load pending state error: {e}")
            self._clear_pending_state()
            return None

    def _restore_pending_session(self, state: dict):
        """Восстанавливает поля сессии из загруженного state."""
        self.patient_id  = state["patient_id"]
        self.notes       = state.get("notes", "")
        self.session_dir = Path(state["session_dir"])
        self.saved_sets  = state.get("saved_sets", [])
        # ── КЛЮЧЕВОЕ: сообщаем NetworkServer'у про незавершённую отправку.
        # sent_files=set() — после жёсткого выключения мы не знаем, какие файлы
        # дошли до ПК. Pi отправит всё. ПК атомарно перепишет дубликаты — это OK
        # (MD5 совпадает, ACK уходит как обычно). ──
        self.net_server._pending_session = (
            self.session_dir, self.patient_id, self.notes, set()
        )
        log(Logger.STARTUP,
            f"Pending session restored | patient='{self.patient_id}' | "
            f"sets={len(self.saved_sets)} | dir={self.session_dir}",
            voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        # Обновим лейблы
        txt = f'ID пациента: "{self.patient_id}"'
        for attr in ("task_title", "finish_title"):
            lbl = getattr(self, attr, None)
            if lbl:
                lbl.config(text=txt)
        # Race-fix: ПК мог успеть подключиться между запуском NetworkServer
        # (в App.__init__) и вызовом _restore_pending_session (через 100мс).
        # Если уже подключён — запускаем retry вручную.
        if self.net_server._client is not None:
            self.root.after(500, self._offer_retry_pending_session)

    # ─────────────────────────────────────────────────────────────────────────
    # SCREEN SWITCHER
    # ─────────────────────────────────────────────────────────────────────────

    def _show(self, name: str):
        for f in self.frames.values():
            f.place_forget()
        self.screen = name
        f = self.frames[name]
        f.place(x=0, y=0, width=W, height=H)
        f.lift()

    # ─────────────────────────────────────────────────────────────────────────
    # SPLASH SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_splash(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["splash"] = f

        self.splash_logo_lbl = tk.Label(f, bg=BG)
        self.splash_logo_lbl.place(relx=0.5, rely=0.35, anchor="center")

        self.splash_bat_lbl = tk.Label(f, bg=BG)
        self.splash_bat_lbl.place(relx=0.5, rely=0.72, anchor="center")

        self.splash_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                    font=("DejaVu Sans", 13))
        self.splash_warn.place(relx=0.5, rely=0.92, anchor="center")

    def _show_splash(self):
        self._show("splash")
        logo_ph = load_asset("logo.png", size=(560, 300))
        if logo_ph:
            self._photo_cache["logo"] = logo_ph
            self.splash_logo_lbl.config(image=logo_ph, text="")
            self.splash_logo_lbl.image = logo_ph
        else:
            self.splash_logo_lbl.config(
                text="HyperspectRus",
                font=("DejaVu Sans", 48, "bold"),
                fg=ACCENT_GRN, image="",
            )
        self._refresh_splash_bat()
        self._refresh_warns()

    # ─────────────────────────────────────────────────────────────────────────
    # WAITING SCREEN  (ожидание подключения ПК и задачи)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_waiting(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["waiting"] = f

        self.wait_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                  font=("DejaVu Sans", 12))
        self.wait_warn.place(x=10, y=6)

        tk.Label(f, text="HyperspectRus",
                 font=("DejaVu Sans", 22, "bold"),
                 fg=ACCENT_GRN, bg=BG,
                 ).place(relx=0.5, rely=0.14, anchor="center")

        # Connection status dot + text
        self.wait_pc_lbl = tk.Label(f, text="○  ПК не подключён",
                                    font=("DejaVu Sans", 20),
                                    fg=TEXT_DIM, bg=BG)
        self.wait_pc_lbl.place(relx=0.5, rely=0.38, anchor="center")

        # Network info
        tk.Label(f,
                 text=f"Сеть: HyperspectRus  IP: {get_local_ip()}",
                 font=("DejaVu Sans", 13),
                 fg=TEXT_DIM, bg=BG,
                 ).place(relx=0.5, rely=0.50, anchor="center")

        # Status message (e.g. "Ожидание задачи…")
        self.wait_status_lbl = tk.Label(f, text="Ожидание подключения ПК…",
                                        font=("DejaVu Sans", 16),
                                        fg="#555555", bg=BG)
        self.wait_status_lbl.place(relx=0.5, rely=0.64, anchor="center")

        # Animated dots
        self.wait_dots = tk.Label(f, text="●  ○  ○",
                                  font=("DejaVu Sans", 22),
                                  fg=BTN_ACTIVE, bg=BG)
        self.wait_dots.place(relx=0.5, rely=0.76, anchor="center")
        self._dot_idx = 0
        self._animate_wait_dots()

    def _animate_wait_dots(self):
        seq = ["●  ○  ○", "○  ●  ○", "○  ○  ●", "○  ●  ○"]
        if hasattr(self, "wait_dots"):
            self.wait_dots.config(text=seq[self._dot_idx % len(seq)])
            self._dot_idx += 1
        self.root.after(600, self._animate_wait_dots)

    def _show_waiting(self):
        self._show("waiting")
        self._refresh_warns()
        # Reflect current PC connection state
        self._update_wait_pc_label()

    def _update_wait_pc_label(self):
        if self.pc_connected:
            self.wait_pc_lbl.config(text="●  ПК подключён", fg=ACCENT_GRN)
            self.wait_status_lbl.config(text="Ожидание задачи от ПК…", fg=TEXT_DIM)
        else:
            self.wait_pc_lbl.config(text="○  ПК не подключён", fg=TEXT_DIM)
            self.wait_status_lbl.config(text="Ожидание подключения ПК…", fg="#555555")

    # ─────────────────────────────────────────────────────────────────────────
    # TASK SELECT SCREEN  (подтверждение задачи с пациентом)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_task_select(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["task_select"] = f

        self.task_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                  font=("DejaVu Sans", 12))
        self.task_warn.place(x=10, y=6)

        # Card
        card = tk.Frame(f, bg=CARD_BG, bd=0)
        card.place(relx=0.5, rely=0.35, anchor="center", width=720, height=200)

        self.task_title = tk.Label(card,
                                   text=f'ID пациента: "{self.patient_id}"',
                                   font=("DejaVu Sans", 32, "bold"),
                                   bg=CARD_BG, fg=CARD_FG)
        self.task_title.place(relx=0.5, rely=0.30, anchor="center")

        tk.Label(card, text="Начать съёмку?",
                 font=("DejaVu Sans", 26),
                 bg=CARD_BG, fg=CARD_FG,
                 ).place(relx=0.5, rely=0.70, anchor="center")

        # ✗ — отклонить задачу, вернуться к ожиданию
        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 54, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._task_cancel,
                  ).place(relx=0.35, rely=0.76, anchor="center", width=160, height=120)

        # ✓ — подтвердить, начать съёмку
        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 54, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._task_confirm,
                  ).place(relx=0.65, rely=0.76, anchor="center", width=160, height=120)

    def _show_task_select(self):
        self.task_title.config(text=f'ID пациента: "{self.patient_id}"')
        self._show("task_select")
        self._refresh_warns()

    def _task_cancel(self):
        """Отклонить задачу — вернуться к ожиданию."""
        self.patient_id = "—"
        self.notes = ""
        self._show_waiting()

    def _task_confirm(self):
        self._show_main()

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN SHOOTING SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_main(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["main"] = f

        # ── Status bar ────────────────────────────────────────────────────────
        sb = tk.Frame(f, bg=STATUS_BG, height=STATUS_H)
        sb.place(x=0, y=0, width=W, height=STATUS_H)

        tk.Label(sb, text="HyperspectRus",
                 font=("DejaVu Sans", 13, "bold"),
                 fg=ACCENT_GRN, bg=STATUS_BG,
                 ).place(x=8, y=5)

        self.main_warn = tk.Label(sb, text="", fg=WARN_COL, bg=STATUS_BG,
                                  font=("DejaVu Sans", 10))
        self.main_warn.place(x=200, y=6)

        self.main_bat_lbl = tk.Label(sb, text="", fg=TEXT_WHITE, bg=STATUS_BG,
                                     font=("DejaVu Sans", 10))
        self.main_bat_lbl.place(x=W - 110, y=2)

        # ── Camera preview ────────────────────────────────────────────────────
        self.main_prev_lbl = tk.Label(f, bg="#0A0A0A",
                                      text="Камера недоступна",
                                      fg=TEXT_DIM, font=("DejaVu Sans", 16))
        self.main_prev_lbl.place(x=0, y=STATUS_H,
                                 width=PREVIEW_W, height=H - STATUS_H)

        # ── Right panel ───────────────────────────────────────────────────────
        BY  = STATUS_H + 10
        BH  = 90
        GAP = 10

        self.btn_rgb = tk.Button(f, text="RGB",
                                 font=("DejaVu Sans", 26, "bold"),
                                 bg=BTN_ACTIVE, fg="#111111",
                                 activebackground=BTN_ACTIVE, activeforeground="#111111",
                                 relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                 command=lambda: self._set_mode(M_RGB))
        self.btn_rgb.place(x=PANEL_X, y=BY, width=PANEL_W, height=BH)

        self.btn_ir = tk.Button(f, text="ИК",
                                font=("DejaVu Sans", 26, "bold"),
                                bg=BTN_IDLE, fg="#111111",
                                activebackground=BTN_IDLE, activeforeground="#111111",
                                relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                command=lambda: self._set_mode(M_IR))
        self.btn_ir.place(x=PANEL_X, y=BY + BH + GAP, width=PANEL_W, height=BH)

        self.btn_nbi = tk.Button(f, text="NBI",
                                 font=("DejaVu Sans", 26, "bold"),
                                 bg=BTN_IDLE, fg="#111111",
                                 activebackground=BTN_IDLE, activeforeground="#111111",
                                 relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                 command=lambda: self._set_mode(M_NBI))
        self.btn_nbi.place(x=PANEL_X, y=BY + 2 * (BH + GAP), width=PANEL_W, height=BH)

        # ── Patient ID label ──────────────────────────────────────────────────
        self.main_pid_lbl = tk.Label(f, text="—",
                                     font=("DejaVu Sans", 12),
                                     fg=TEXT_DIM, bg=BG)
        self.main_pid_lbl.place(x=PANEL_X, y=BY + 3 * (BH + GAP),
                                width=PANEL_W, height=30)

        # ── Finish button ─────────────────────────────────────────────────────
        tk.Button(f, text="Завершить",
                  font=("DejaVu Sans", 20, "bold"),
                  bg=BTN_FINISH, fg="#DDDDDD",
                  activebackground=BTN_FINISH, activeforeground="#DDDDDD",
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._show_finish_confirm,
                  ).place(x=PANEL_X - 4, y=H - 115, width=PANEL_W + 8, height=106)

    def _show_main(self):
        self.main_pid_lbl.config(text=f"ID: {self.patient_id}")
        self._show("main")
        self._refresh_warns()
        self._refresh_battery()
        self._set_mode(self.led_mode)
        self._start_cam_preview()

    def _set_mode(self, mode: str):
        self.led_mode = mode
        for btn, m in [(self.btn_rgb, M_RGB), (self.btn_ir, M_IR), (self.btn_nbi, M_NBI)]:
            c = BTN_ACTIVE if mode == m else BTN_IDLE
            btn.config(bg=c, activebackground=c)
        if self.stm.connected:
            threading.Thread(
                target=self.stm.set_preview_leds,
                args=(mode, PREVIEW_LED_DUTY),
                daemon=True,
            ).start()

    # ─────────────────────────────────────────────────────────────────────────
    # FINISH CONFIRM SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_finish_confirm(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["finish_confirm"] = f

        self.finish_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                    font=("DejaVu Sans", 12))
        self.finish_warn.place(x=10, y=6)

        card = tk.Frame(f, bg=CARD_BG, bd=0)
        card.place(relx=0.5, rely=0.33, anchor="center", width=720, height=210)

        self.finish_title = tk.Label(card,
                                     text=f'ID пациента: "{self.patient_id}"',
                                     font=("DejaVu Sans", 32, "bold"),
                                     bg=CARD_BG, fg=CARD_FG)
        self.finish_title.place(relx=0.5, rely=0.30, anchor="center")

        tk.Label(card, text="Завершить съёмку?",
                 font=("DejaVu Sans", 26),
                 bg=CARD_BG, fg=CARD_FG,
                 ).place(relx=0.5, rely=0.70, anchor="center")

        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 54, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._finish_cancel,
                  ).place(relx=0.35, rely=0.76, anchor="center", width=160, height=120)

        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 54, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._finish_ok,
                  ).place(relx=0.65, rely=0.76, anchor="center", width=160, height=120)

    def _show_finish_confirm(self):
        self.finish_title.config(text=f'ID пациента: "{self.patient_id}"')
        self._show("finish_confirm")
        self._refresh_warns()

    def _finish_cancel(self):
        # Если на экране восстановленной сессии (после старта) — пользователь
        # отказался отправлять. Удаляем папку и pending state.
        if self.patient_id == "—" or not self.pc_connected:
            # Похоже на восстановленную сессию или нет активной задачи
            if self.session_dir is not None:
                try:
                    import shutil
                    shutil.rmtree(self.session_dir, ignore_errors=True)
                except Exception:
                    pass
                self._clear_pending_state()
                self._reset_session()
                self.patient_id = "—"
                self._show_waiting()
                return
        self._show_main()

    def _finish_ok(self):
        """Send all saved photos to PC, then go to waiting screen."""
        self._stop_cam_preview()
        threading.Thread(target=self.stm.all_off, daemon=True).start()
        if self.session_dir is not None and self.saved_sets:
            # Count files first so we can show total on the progress screen
            sd = self.session_dir
            try:
                total = sum(1 for f in sd.rglob("*") if f.is_file())
            except Exception:
                total = 0
            self._show_sending(total)
            threading.Thread(target=self._send_all_photos, daemon=True).start()
        else:
            self._reset_session()
            self._show_waiting()

    def _send_all_photos(self):
        sd = self.session_dir
        mv = self.stm.get_battery_mv() if self.stm_ok else -1
        try:
            total_files = sum(1 for f in sd.rglob("*") if f.is_file())
        except Exception:
            total_files = 0
        log(Logger.SEND_START,
            f"patient='{self.patient_id}' files={total_files} dir={sd}",
            voltage_mv=mv)

        def _progress(current, total, filename):
            self.root.after(0, self._update_send_progress, current, total, filename)
            log(Logger.SEND_FILE,
                f"{current}/{total} {filename}",
                voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)

        ok = self.net_server.send_photos(sd, self.patient_id, self.notes,
                                         progress_cb=_progress)
        mv_after = self.stm.get_battery_mv() if self.stm_ok else -1
        if ok:
            log(Logger.SEND_OK,
                f"patient='{self.patient_id}' files={total_files} all ACKed",
                voltage_mv=mv_after)
            # Сессия успешно отправлена — удаляем папку и pending state
            self._clear_pending_state()
            try:
                import shutil
                shutil.rmtree(sd, ignore_errors=True)
                print(f"Deleted local session: {sd}")
            except Exception as e:
                print(f"Cleanup error: {e}")
            self._reset_session()
            self.root.after(0, self._show_waiting)
        else:
            log(Logger.SEND_FAIL,
                f"patient='{self.patient_id}' transfer interrupted — will retry on reconnect",
                voltage_mv=mv_after)
            # НЕ сбрасываем сессию — pending_session в NetworkServer ждёт реконнекта,
            # а наш state на диске позволит восстановиться даже после перезапуска.
            # Просто покажем экран ожидания PC.
            self.root.after(0, self._show_waiting)

    def _reset_session(self):
        self.saved_sets  = []
        self.session_dir = None

    # ─────────────────────────────────────────────────────────────────────────
    # SENDING PROGRESS SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sending(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["sending"] = f

        tk.Label(f, text="Отправка на ПК…",
                 font=("DejaVu Sans", 40, "bold"),
                 fg=ACCENT_GRN, bg=BG,
                 ).place(relx=0.5, rely=0.22, anchor="center")

        # File count label  e.g.  "Файл 3 из 16"
        self.send_count_lbl = tk.Label(f, text="",
                                       font=("DejaVu Sans", 22),
                                       fg=TEXT_WHITE, bg=BG)
        self.send_count_lbl.place(relx=0.5, rely=0.42, anchor="center")

        # Current filename
        self.send_file_lbl = tk.Label(f, text="",
                                      font=("DejaVu Sans", 14),
                                      fg=TEXT_DIM, bg=BG)
        self.send_file_lbl.place(relx=0.5, rely=0.53, anchor="center")

        # Progress bar track
        BAR_W, BAR_H = 640, 28
        self._send_bar_w = BAR_W
        track = tk.Canvas(f, width=BAR_W, height=BAR_H,
                          bg="#1E1E1E", highlightthickness=0, bd=0)
        track.place(relx=0.5, rely=0.65, anchor="center")
        self._send_bar_canvas = track
        self._send_bar_rect   = track.create_rectangle(
            0, 0, 0, BAR_H, fill=BTN_ACTIVE, outline="")

        # Percent label
        self.send_pct_lbl = tk.Label(f, text="0%",
                                     font=("DejaVu Sans", 18, "bold"),
                                     fg=ACCENT_GRN, bg=BG)
        self.send_pct_lbl.place(relx=0.5, rely=0.77, anchor="center")

    def _show_sending(self, total_files: int):
        self.send_count_lbl.config(text=f"0 из {total_files} файлов")
        self.send_file_lbl.config(text="")
        self.send_pct_lbl.config(text="0%")
        self._send_bar_canvas.coords(self._send_bar_rect, 0, 0, 0, 28)
        self._show("sending")

    def _update_send_progress(self, current: int, total: int, filename: str):
        """Called from background thread via root.after."""
        if total == 0:
            pct = 0
        else:
            pct = int(current / total * 100)
        bar_px = int(self._send_bar_w * pct / 100)
        self.send_count_lbl.config(text=f"{current} из {total} файлов")
        self.send_file_lbl.config(text=filename)
        self.send_pct_lbl.config(text=f"{pct}%")
        self._send_bar_canvas.coords(self._send_bar_rect, 0, 0, bar_px, 28)

    # ─────────────────────────────────────────────────────────────────────────
    # CAPTURE SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_capturing(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["capturing"] = f

        tk.Label(f, text="Идёт съёмка…",
                 font=("DejaVu Sans", 46, "bold"),
                 fg=ACCENT_GRN, bg=BG,
                 ).place(relx=0.5, rely=0.36, anchor="center")

        self.cap_progress = tk.Label(f, text="",
                                     font=("DejaVu Sans", 22),
                                     fg=TEXT_DIM, bg=BG)
        self.cap_progress.place(relx=0.5, rely=0.58, anchor="center")

        self.cap_led_lbl = tk.Label(f, text="",
                                    font=("DejaVu Sans", 18),
                                    fg="#7B9A2A", bg=BG)
        self.cap_led_lbl.place(relx=0.5, rely=0.72, anchor="center")

        self.cap_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                 font=("DejaVu Sans", 12))
        self.cap_warn.place(x=10, y=6)

    def _shutter_pressed(self):
        if self.screen == "main":
            self._begin_capture()

    def _begin_capture(self):
        self._stop_cam_preview()
        threading.Thread(target=self.stm.all_off, daemon=True).start()
        self._show("capturing")
        self.cap_progress.config(text="Подготовка…")
        self.cap_led_lbl.config(text="")
        log(Logger.PHOTO,
            f"Capture started | patient='{self.patient_id}' | leds={len(LED_TABLE)}",
            voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
        threading.Thread(target=self._capture_sequence, daemon=True).start()

    def _capture_sequence(self):
        images   = []   # list of (wl, PIL.Image) — for preview after capture
        raw_bufs = []   # list of (wl, bytes)     — DNG data, only in raw mode

        if CAM_OK and self.cam:
            try:
                for i, (led, wl, duty) in enumerate(LED_TABLE):
                    if self.stm.connected:
                        self.stm.led_duty(led, duty)

                if self.cam_running:
                    self.cam.stop()
                    self.cam_running = False

                # Choose config and apply capture exposure
                cap_cfg = self._cfg_raw if CAPTURE_FORMAT == "raw" else self._cfg_jpeg
                self.cam.configure(cap_cfg)
                self._apply_capture_settings()

                self.cam.start()
                self.cam_running = True
                time.sleep(0.1)

                # ── PHASE 1: fast shooting — only grab buffers, release ASAP ──
                shot_data = []   # (wl, main_arr, raw_arr_or_None, metadata)
                for i, (led, wl, duty) in enumerate(LED_TABLE):
                    mv_now = self.stm.get_battery_mv() if self.stm_ok else -1

                    if self.stm.connected:
                        self.stm.led_on(led)
                    log(Logger.LED_ON,
                        f"led={led} wl={wl}nm duty={duty}%",
                        voltage_mv=mv_now)

                    self.root.after(0, lambda m=f"Снимок {i+1}/{len(LED_TABLE)}  —  {wl} нм":
                                    self.cap_progress.config(text=m))
                    self.root.after(0, lambda m=f"LED {led}  ·  {duty}% PWM":
                                    self.cap_led_lbl.config(text=m))

                    for _ in range(2):
                        throwaway = self.cam.capture_request()
                        throwaway.release()

                    req      = self.cam.capture_request()
                    main_arr = req.make_array("main").copy()
                    raw_arr  = req.make_array("raw").copy() if CAPTURE_FORMAT == "raw" else None
                    metadata = req.get_metadata()
                    req.release()                            # ← release immediately

                    if self.stm.connected:
                        self.stm.led_off(led)
                    log(Logger.LED_OFF,
                        f"led={led} wl={wl}nm | shot captured",
                        voltage_mv=mv_now)
                    log(Logger.PHOTO,
                        f"Shot {i+1}/{len(LED_TABLE)} | wl={wl}nm | exposure={CAPTURE_EXPOSURE_US}µs",
                        voltage_mv=mv_now)

                    shot_data.append((wl, main_arr, raw_arr, metadata))

                self.cam.stop()
                self.cam_running = False

                self.root.after(0, lambda: self.cap_progress.config(text="Обработка…"))
                self.root.after(0, lambda: self.cap_led_lbl.config(text=""))

                # Restore preview config for next session
                self.cam.configure(self._cfg_jpeg)

                # ── PHASE 2: post-capture encoding (off the hot path) ──────────
                for wl, main_arr, raw_arr, meta in shot_data:
                    # JPEG via helpers (PIL only here, not in realtime)
                    pil_img = self.cam.helpers.make_image(main_arr, cap_cfg["main"])
                    images.append((wl, pil_img))

                    # DNG via helpers
                    if CAPTURE_FORMAT == "raw" and raw_arr is not None:
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".dng")
                        try:
                            os.close(tmp_fd)
                            self.cam.helpers.save_dng(
                                raw_arr, meta, cap_cfg["raw"], tmp_path
                            )
                            raw_bufs.append((wl, Path(tmp_path).read_bytes()))
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except Exception:
                                pass

            except Exception as e:
                print(f"Capture error: {e}")
                log(Logger.ERROR,
                    f"Capture error: {e}",
                    voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)
                images   = self._dummy_images()
                raw_bufs = []
        else:
            images = self._dummy_images()

        self.captures  = images
        self.raw_bufs  = raw_bufs   # stored for _prev_save
        self.prev_idx  = 0
        self.root.after(0, self._show_photo_preview)

    def _dummy_images(self):
        colors = [
            (30, 60, 200), (20, 180, 60), (200, 40, 40), (140, 60, 140),
            (150, 70, 110), (130, 80, 90), (110, 90, 70), (90, 100, 50),
        ]
        imgs = []
        for (led, wl, duty), col in zip(LED_TABLE, colors):
            img = Image.new("RGB", (640, 480), color=col)
            draw = ImageDraw.Draw(img)
            draw.text((20, 20), f"{wl} nm  LED{led}", fill=(255, 255, 255))
            imgs.append((wl, img))
            time.sleep(0.15)
        return imgs

    # ─────────────────────────────────────────────────────────────────────────
    # PHOTO PREVIEW SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_photo_preview(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["photo_preview"] = f

        self.prev_photo_lbl = tk.Label(f, bg="#0A0A0A", text="",
                                       fg=TEXT_DIM, font=("DejaVu Sans", 14))
        self.prev_photo_lbl.place(x=0, y=0, width=PREVIEW_W, height=H)

        px, pw = PANEL_X, PANEL_W
        HALF   = (pw - 6) // 2

        # Wavelength label
        self.prev_wl_lbl = tk.Label(f, text="450 нм",
                                    font=("DejaVu Sans", 26, "bold"),
                                    fg=TEXT_WHITE, bg=BG)
        self.prev_wl_lbl.place(x=px, y=10, width=pw, height=44)

        self.prev_cnt_lbl = tk.Label(f, text="1 / 8",
                                     font=("DejaVu Sans", 16),
                                     fg=TEXT_DIM, bg=BG)
        self.prev_cnt_lbl.place(x=px, y=56, width=pw, height=28)

        # Navigation
        tk.Button(f, text="◀",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._prev_left,
                  ).place(x=px, y=96, width=HALF, height=100)

        tk.Button(f, text="▶",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._prev_right,
                  ).place(x=px + HALF + 6, y=96, width=HALF, height=100)

        # Discard / Save
        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 46, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._prev_discard,
                  ).place(x=px, y=322, width=HALF, height=150)

        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 46, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  command=self._prev_save,
                  ).place(x=px + HALF + 6, y=322, width=HALF, height=150)

    def _show_photo_preview(self):
        self._show("photo_preview")
        self._refresh_preview_image()

    def _refresh_preview_image(self):
        if not self.captures:
            return
        wl, img = self.captures[self.prev_idx]
        cropped = resize_to_fill(img, PREVIEW_W, H)
        ph = ImageTk.PhotoImage(cropped)
        self._photo_cache["prev_img"] = ph
        self.prev_photo_lbl.config(image=ph, text="")
        self.prev_photo_lbl.image = ph
        self.prev_wl_lbl.config(text=f"{wl} нм")
        self.prev_cnt_lbl.config(text=f"{self.prev_idx + 1} / {len(self.captures)}")

    def _prev_left(self):
        if self.captures:
            self.prev_idx = (self.prev_idx - 1) % len(self.captures)
            self._refresh_preview_image()

    def _prev_right(self):
        if self.captures:
            self.prev_idx = (self.prev_idx + 1) % len(self.captures)
            self._refresh_preview_image()

    def _prev_discard(self):
        self.captures = []
        self._show_main()

    def _prev_save(self):
        """Save captured images into a numbered set folder inside the patient session."""
        # One patient session dir per task (created on first save, reused on subsequent)
        if self.session_dir is None:
            ts      = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_id = self.patient_id.replace(" ", "_").replace("/", "_")
            self.session_dir = SESSIONS_DIR / f"{safe_id}_{ts}"

        session = self.session_dir

        # Next set number = how many sets already saved + 1
        set_num = len(self.saved_sets) + 1

        captures_copy = list(self.captures)
        raw_copy      = list(getattr(self, "raw_bufs", []))

        def _save():
            # jpeg/<set_num>/
            jpeg_set_dir = session / "jpeg" / str(set_num)
            jpeg_set_dir.mkdir(parents=True, exist_ok=True)
            for wl, img in captures_copy:
                img.save(jpeg_set_dir / f"{wl}nm.jpg", format="JPEG", quality=95)
                log(Logger.FILE_SAVED,
                    f"set={set_num} jpeg/{set_num}/{wl}nm.jpg",
                    voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)

            # raw/<set_num>/  — only in RAW mode
            if CAPTURE_FORMAT == "raw" and raw_copy:
                raw_set_dir = session / "raw" / str(set_num)
                raw_set_dir.mkdir(parents=True, exist_ok=True)
                for wl, dng_bytes in raw_copy:
                    (raw_set_dir / f"{wl}nm.dng").write_bytes(dng_bytes)
                    log(Logger.FILE_SAVED,
                        f"set={set_num} raw/{set_num}/{wl}nm.dng ({len(dng_bytes)//1024} KB)",
                        voltage_mv=self.stm.get_battery_mv() if self.stm_ok else -1)

            print(f"Saved set {set_num} → {session}  (format={CAPTURE_FORMAT})")
            # Сохраняем state ПОСЛЕ записи всех файлов (чтобы при крахе во время записи
            # state не указывал на несуществующие файлы)
            self.root.after(0, self._save_pending_state)

        threading.Thread(target=_save, daemon=True).start()
        self.saved_sets.append(set_num)   # track count, not path
        self.captures = []
        self.raw_bufs = []
        self._show_main()

    # ─────────────────────────────────────────────────────────────────────────
    # BOOT
    # ─────────────────────────────────────────────────────────────────────────

    def _boot(self):
        # Проверяем есть ли незавершённая сессия от прошлого запуска
        pending = self._load_pending_state()
        if pending:
            print(f"Found pending session: {pending['session_dir']} "
                  f"with {pending['file_count']} files")
            self._restore_pending_session(pending)
            # Покажем splash на секунду, потом сразу к экрану finish_confirm
            # чтобы пользователь мог решить — отправить или продолжить съёмку.
            self._show_splash()
            self.root.after(1500, self._show_recovered_session)
            return

        if self.charging in (1, 2):
            self._show_splash()
        else:
            self._show_splash()
            self.root.after(1500, self._show_waiting)

    def _show_recovered_session(self):
        """После старта с восстановленной сессией.
        Если ПК уже подключён — retry уже идёт, показываем экран отправки.
        Иначе показываем finish_confirm чтобы юзер мог дождаться ПК или отменить."""
        if self.charging in (1, 2):
            self._show_splash()
            return
        if self.pc_connected:
            # retry запущен в _restore_pending_session → _offer_retry_pending_session
            try:
                total = sum(1 for f in self.session_dir.rglob("*") if f.is_file())
            except Exception:
                total = 0
            self._show_sending(total)
        else:
            # ПК нет — даём юзеру увидеть что есть несохранённая сессия.
            # Когда ПК подключится, _offer_retry_pending_session переключит экран сам.
            self._show_finish_confirm()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App()