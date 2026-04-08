#!/usr/bin/env python3
"""
HyperspectRus — Hyperspectral Camera App
Raspberry Pi Zero + Picamera2 + STM32 + 800x480 touchscreen
"""

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
PREVIEW_W    = 490
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
# STM32 CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════════

class STM32:
    PORTS   = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"]
    BAUD    = 9600
    TIMEOUT = 0.5

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
                deadline = time.time() + self.TIMEOUT
                buf = b""
                while time.time() < deadline:
                    chunk = self.ser.read(self.ser.in_waiting or 1)
                    if chunk:
                        buf += chunk
                        if b"\n" in buf:
                            break
                    else:
                        time.sleep(0.005)
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

def mv_to_pct(mv: int) -> int:
    if mv <= 0:
        return 0
    pct = int((mv - 3200) / (4200 - 3200) * 100)
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
        self.app      = app
        self._running = True
        self._sock    = None
        self._client  = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((SERVER_HOST, SERVER_PORT))
            self._sock.listen(1)
            self._sock.settimeout(1.0)
            print(f"Network server listening on {SERVER_PORT}")
        except Exception as e:
            print(f"Server bind error: {e}")
            return

        while self._running:
            try:
                conn, addr = self._sock.accept()
                self._client = conn
                print(f"PC connected from {addr}")
                self.app.root.after(0, lambda: self.app.set_pc_connected(True))
                self._handle(conn)
                self.app.root.after(0, lambda: self.app.set_pc_connected(False))
                self._client = None
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Server accept error: {e}")
                time.sleep(1)

    def _handle(self, conn: socket.socket):
        buf = b""
        conn.settimeout(2.0)
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
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

    def _send(self, conn, data: dict):
        try:
            line = json.dumps(data).encode("utf-8") + b"\n"
            conn.sendall(line)
        except Exception as e:
            print(f"Send error: {e}")

    def send_photos(self, session_dir: Path, patient_id: str, notes: str,
                    progress_cb=None):
        """Send all photos from session_dir to connected PC using relative paths."""
        conn = self._client
        if not conn:
            print("No PC connected — cannot send photos")
            return False
        try:
            # Wait a moment for any background _save threads to finish writing
            time.sleep(0.5)

            files = sorted(
                f for f in session_dir.rglob("*") if f.is_file()
            )
            total = len(files)
            meta = {
                "cmd":        "session_start",
                "patient_id": patient_id,
                "notes":      notes,
                "file_count": total,
            }
            self._send(conn, meta)
            time.sleep(0.1)

            for idx, f in enumerate(files, start=1):
                data     = f.read_bytes()
                rel_path = f.relative_to(session_dir).as_posix()
                if progress_cb:
                    progress_cb(idx, total, rel_path)
                header   = json.dumps({
                    "cmd":      "file",
                    "filename": rel_path,
                    "size":     len(data),
                }).encode("utf-8") + b"\n"
                conn.sendall(header)
                conn.sendall(data)
                time.sleep(0.02)

            self._send(conn, {"cmd": "session_end"})
            return True
        except Exception as e:
            print(f"Photo send error: {e}")
            return False


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
        self.saved_sets   = []       # list of session dirs to send
        self.pc_connected = False
        self._preview_job = None

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

            # JPEG-only config (used for preview and jpeg capture)
            self._cfg_jpeg = self.cam.create_still_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
            )

            # RAW config: main stream for jpeg preview + raw stream for DNG
            self._cfg_raw = self.cam.create_still_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
                raw={},   # picamera2 picks sensor native format automatically
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
            print(f"Camera initialized (1280x720, format={CAPTURE_FORMAT})")
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
            img = Image.fromarray(arr)
            cropped = resize_to_fill(img, PREVIEW_W, H - STATUS_H)
            ph = ImageTk.PhotoImage(cropped)
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

                    if chg >= 0 and chg != old_chg:
                        self.root.after(0, self._on_charge_changed)

            except Exception as e:
                print(f"Battery thread error: {e}")
            time.sleep(5)

    def _on_charge_changed(self):
        if self.charging in (1, 2):
            if self.screen != "splash":
                self._stop_cam_preview()
                threading.Thread(target=self.stm.all_off, daemon=True).start()
                self._show_splash()
        else:
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
        if self.screen == "waiting":
            self._update_wait_pc_label()

    def receive_task(self, patient_id: str, notes: str):
        self.patient_id  = patient_id
        self.notes       = notes
        self.saved_sets  = []
        self.session_dir = None
        # Update labels
        txt = f'ID пациента: "{patient_id}"'
        for attr in ("task_title", "finish_title"):
            lbl = getattr(self, attr, None)
            if lbl:
                lbl.config(text=txt)
        # Always jump to task confirm screen when a task arrives
        self._show_task_select()

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
                 text="Сеть: HyperspectRus  ·  172.20.10.2",
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

        def _progress(current, total, filename):
            self.root.after(0, self._update_send_progress, current, total, filename)

        ok = self.net_server.send_photos(sd, self.patient_id, self.notes,
                                         progress_cb=_progress)
        if ok:
            # Delete session folder from Pi to free memory
            try:
                import shutil
                shutil.rmtree(sd, ignore_errors=True)
                print(f"Deleted local session: {sd}")
            except Exception as e:
                print(f"Cleanup error: {e}")
        self._reset_session()
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
        threading.Thread(target=self._capture_sequence, daemon=True).start()

    def _capture_sequence(self):
        images   = []   # list of (wl, PIL.Image) — always for preview
        raw_bufs = []   # list of (wl, bytes) — DNG data, only in raw mode

        if CAM_OK and self.cam:
            try:
                if self.cam_running:
                    self.cam.stop()
                    self.cam_running = False

                # Switch to correct config and apply capture exposure
                cfg = self._cfg_raw if CAPTURE_FORMAT == "raw" else self._cfg_jpeg
                self.cam.configure(cfg)
                self._apply_capture_settings()

                self.cam.start()
                self.cam_running = True
                time.sleep(0.3)

                for i, (led, wl, duty) in enumerate(LED_TABLE):
                    self.root.after(0, lambda m=f"Снимок {i+1}/{len(LED_TABLE)}  —  {wl} нм":
                                    self.cap_progress.config(text=m))
                    self.root.after(0, lambda m=f"LED {led}  ·  {duty}% PWM":
                                    self.cap_led_lbl.config(text=m))

                    if self.stm.connected:
                        self.stm.led_duty(led, duty)
                        self.stm.led_on(led)
                        time.sleep(0.05)

                    req = self.cam.capture_request()

                    # Always grab JPEG from main stream for preview
                    jpeg_buf = io.BytesIO()
                    req.save("main", jpeg_buf, format="jpeg")

                    # In RAW mode also grab the DNG from raw stream
                    if CAPTURE_FORMAT == "raw":
                        import tempfile, os as _os
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".dng")
                        try:
                            _os.close(tmp_fd)
                            req.save_dng(tmp_path)
                            raw_bufs.append((wl, Path(tmp_path).read_bytes()))
                        finally:
                            try:
                                _os.unlink(tmp_path)
                            except Exception:
                                pass

                    req.release()

                    if self.stm.connected:
                        self.stm.led_off(led)

                    jpeg_buf.seek(0)
                    img = Image.open(jpeg_buf)
                    img.load()
                    images.append((wl, img.copy()))

                self.cam.stop()
                self.cam_running = False

                # Restore JPEG config for next preview session
                self.cam.configure(self._cfg_jpeg)

            except Exception as e:
                print(f"Capture error: {e}")
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
            self.session_dir = Path("sessions") / f"{safe_id}_{ts}"

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

            # raw/<set_num>/  — only in RAW mode
            if CAPTURE_FORMAT == "raw" and raw_copy:
                raw_set_dir = session / "raw" / str(set_num)
                raw_set_dir.mkdir(parents=True, exist_ok=True)
                for wl, dng_bytes in raw_copy:
                    (raw_set_dir / f"{wl}nm.dng").write_bytes(dng_bytes)

            print(f"Saved set {set_num} → {session}  (format={CAPTURE_FORMAT})")

        threading.Thread(target=_save, daemon=True).start()
        self.saved_sets.append(set_num)   # track count, not path
        self.captures = []
        self.raw_bufs = []
        self._show_main()

    # ─────────────────────────────────────────────────────────────────────────
    # BOOT
    # ─────────────────────────────────────────────────────────────────────────

    def _boot(self):
        if self.charging in (1, 2):
            self._show_splash()
        else:
            self._show_splash()
            self.root.after(1500, self._show_waiting)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App()