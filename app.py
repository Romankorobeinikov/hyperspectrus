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
import sys
from pathlib import Path

# ── PIL ──────────────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("WARNING: Pillow not installed")

# ── Serial ───────────────────────────────────────────────────────────────────
try:
    import serial
    import serial.tools.list_ports
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False
    print("WARNING: pyserial not installed")

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

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

W, H        = 800, 480
ASSETS      = Path("assets")
PATIENT_ID  = "022"
PREVIEW_W   = 480          # camera preview width
PANEL_X     = PREVIEW_W + 8
PANEL_W     = W - PREVIEW_W - 16
STATUS_H    = 34

# ── Camera preview ───────────────────────────────────────────────────────────
PREVIEW_FPS = 30

# LED config  (led_index, wavelength_nm, capture_duty%)
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
PREVIEW_DUTY = 40   # % PWM during preview
DEFAULT_DUTY = 20   # % PWM default setting

# Modes
M_RGB  = "rgb"
M_IR   = "ir"
M_NBI  = "nbi"

# ── Palette ───────────────────────────────────────────────────────────────────
BG          = "#000000"
STATUS_BG   = "#000000"
CARD_BG     = "#D4D4D4"
CARD_FG     = "#1A1A1A"
BTN_IDLE    = "#CECECE"
BTN_ACTIVE  = "#6E9B1E"
BTN_FINISH  = "#767676"
BTN_DANGER  = "#B03030"
BTN_OK      = "#4A8A18"
TEXT_WHITE  = "#FFFFFF"
TEXT_DIM    = "#888888"
ACCENT_GRN  = "#6FCF3A"
ACCENT_ORG  = "#FF8C00"
WARN_COL    = "#FF6600"


# ═══════════════════════════════════════════════════════════════════════════════
# STM32 CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════════

class STM32:
    PORTS   = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"]
    BAUD    = 115200
    TIMEOUT = 0.5

    def __init__(self):
        self.ser       = None
        self.connected = False
        self._lock     = threading.Lock()
        self._connect()

    # ── internal ────────────────────────────────────────────────────────────

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
        # fallback: scan all comports
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

    def reset_device(self):
        self._cmd("resetDevice\n")

    # ── public API ───────────────────────────────────────────────────────────

    def get_charging_state(self) -> int:
        """0 = not charging, 1 = charging, 2 = full"""
        r = self._cmd("getChargingState\n")
        try:
            return int(r)
        except Exception:
            return -1

    def get_battery_mv(self) -> int:
        """Battery voltage in mV"""
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

    def set_preview_leds(self, mode: str, duty: int = PREVIEW_DUTY):
        self.all_off()
        if mode == M_RGB:
            for led in [1, 2, 3]:
                self.led_duty(led, duty)
                self.led_on(led)
        elif mode == M_IR:
            self.led_duty(8, duty)
            self.led_on(8)
        elif mode == M_NBI:
            for led in [1, 2]:
                self.led_duty(led, duty)
                self.led_on(led)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def mv_to_pct(mv: int) -> int:
    """3000 mV = 0%, 4200 mV = 100%"""
    if mv <= 0:
        return 0
    pct = int((mv - 3000) / (4200 - 3000) * 100)
    return max(0, min(100, pct))


def load_asset(name: str, size=None) -> "ImageTk.PhotoImage | None":
    """Load image from assets/, optionally resize."""
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
    """
    bat1–bat5  : 0/25/50/75/100% not charging
    bat6–bat10 : same with lightning (charging)
    """
    thresholds = [0, 25, 50, 75, 100]
    idx = min(range(len(thresholds)), key=lambda i: abs(thresholds[i] - pct))
    offset = 5 if charging in (1, 2) else 0
    return f"bat{idx + 1 + offset}.jpg"


def make_button(parent, text, font_obj, bg, fg, cmd, **place_kw):
    btn = tk.Button(
        parent, text=text, font=font_obj,
        bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
        relief="flat", bd=0, highlightthickness=0,
        cursor="hand2",
        command=cmd,
    )
    btn.place(**place_kw)
    return btn

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

class App:

    def __init__(self):
        # ── State ────────────────────────────────────────────────────────────
        self.screen        = None
        self.led_mode      = M_RGB
        self.led_duty_val  = DEFAULT_DUTY
        self.battery_pct   = 0
        self.charging      = 0
        self.stm_ok        = False
        self.cam_running   = False
        self.captures      = []   # list of (wl, PIL.Image)
        self.prev_idx      = 0
        self._preview_job  = None
        self._bat_job      = None

        # ── Hardware ─────────────────────────────────────────────────────────
        self.stm    = STM32()
        self.stm_ok = self.stm.connected
        self.cam    = None
        self._init_camera()

        # ── Root window ──────────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("HyperspectRus")
        self.root.geometry(f"{W}x{H}+0+0")
        self.root.configure(bg=BG)
        self.root.attributes("-fullscreen", True)
        self.root.resizable(False, False)
        self.root.option_add("*tearOff", False)
        self.root.config(cursor="none")

        # ── Fonts ────────────────────────────────────────────────────────────
        self.fnt_xl   = tkfont.Font(family="DejaVu Sans", size=40, weight="bold")
        self.fnt_lg   = tkfont.Font(family="DejaVu Sans", size=28, weight="bold")
        self.fnt_md   = tkfont.Font(family="DejaVu Sans", size=20)
        self.fnt_mdb  = tkfont.Font(family="DejaVu Sans", size=20, weight="bold")
        self.fnt_sm   = tkfont.Font(family="DejaVu Sans", size=13)
        self.fnt_smb  = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")

        # ── Asset cache ──────────────────────────────────────────────────────
        self._photo_cache = {}

        # ── Frames ───────────────────────────────────────────────────────────
        self.frames = {}
        self._build_splash()
        self._build_task_select()
        self._build_main()
        self._build_finish_confirm()
        self._build_capturing()
        self._build_preview()

        # ── GPIO shutter button ───────────────────────────────────────────────
        if GPIO_OK and _gpio_btn:
            _gpio_btn.when_pressed = lambda: self.root.after(0, self._shutter_pressed)

        # ── Background threads ────────────────────────────────────────────────
        threading.Thread(target=self._battery_thread, daemon=True).start()

        # ── First screen ─────────────────────────────────────────────────────
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

            self.fixed_config = self.cam.create_still_configuration(
                main={
                    "size": (1280, 720),
                    "format": "RGB888"
                }
            )

            self.cam.configure(self.fixed_config)

            self.cam.set_controls({
                "AeEnable": False,
                "AwbEnable": False,
                "ExposureTime": 1000,       
                "AnalogueGain": 1.1,
                "ColourGains": (1.0, 1.0),
                "AfMode": libcontrols.AfModeEnum.Manual,
                "LensPosition": 12.0,
            })

            print("Camera initialized with fixed parameters (1280x720)")
        except Exception as e:
            print(f"Camera init error: {e}")
            self.cam = None

    def _resize_to_fill(self, img: Image.Image, target_w: int, target_h: int) -> Image.Image:
        """Заполняет всю целевую область по высоте, обрезая лишнее слева/справа по центру"""
        # 1. Вычисляем масштаб, чтобы высота стала ровно target_h
        scale = target_h / img.height
        new_w = int(img.width * scale)

        # 2. Сначала ресайзим (с сохранением пропорций)
        resized = img.resize((new_w, target_h), Image.LANCZOS)

        # 3. Обрезаем по центру до нужной ширины
        if new_w > target_w:
            left = (new_w - target_w) // 2
            right = left + target_w
            resized = resized.crop((left, 0, right, target_h))

        return resized

    def _start_cam_preview(self):
        if not self.cam or self.cam_running:
            return
        try:
            self.cam.start()
            self.cam_running = True
            self._schedule_preview()
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

    def _schedule_preview(self):
        if self.screen == "main" and self.cam_running:
            self._do_preview_frame()

    def _do_preview_frame(self):
        if self.screen != "main" or not self.cam_running:
            return
        try:
            arr = self.cam.capture_array()
            img = Image.fromarray(arr)

            cropped = self._resize_to_fill(img, PREVIEW_W, H - STATUS_H)
            ph = ImageTk.PhotoImage(cropped)

            self.main_prev_lbl.config(image=ph, text="")
            self.main_prev_lbl.image = ph
        except Exception:
            pass
        delay_ms = max(1, int(1000 / PREVIEW_FPS)) 
        self._preview_job = self.root.after(delay_ms, self._do_preview_frame)

    # ─────────────────────────────────────────────────────────────────────────
    # BATTERY BACKGROUND THREAD
    # ─────────────────────────────────────────────────────────────────────────

    def _battery_thread(self):
        while True:
            try:
                if not self.stm.connected:
                    self.stm.reconnect()
                    self.stm_ok = self.stm.connected
                    self.root.after(0, self._refresh_warn_labels)

                if self.stm.connected:
                    mv  = self.stm.get_battery_mv()
                    chg = self.stm.get_charging_state()

                    pct = mv_to_pct(mv) if mv > 0 else self.battery_pct

                    changed_chg = (chg >= 0 and chg != self.charging)

                    self.battery_pct = pct
                    if chg >= 0:
                        self.charging = chg

                    self.root.after(0, self._refresh_battery_display)

                    if changed_chg:
                        self.root.after(0, self._on_charge_changed)

            except Exception as e:
                print(f"Battery thread error: {e}")

            time.sleep(5)

    def _on_charge_changed(self):
        if self.charging in (1, 2):
            # Plugged in → standby
            if self.screen != "splash":
                self._stop_cam_preview()
                threading.Thread(target=self.stm.all_off, daemon=True).start()
                self._show_splash()
        else:
            # Unplugged → activate
            if self.screen == "splash":
                self._show_task_select()

    def _refresh_battery_display(self):
        icon_name = battery_icon_name(self.battery_pct, self.charging)
        ph = load_asset(icon_name, size=(72, 35))
        if ph:
            self._photo_cache["bat_icon"] = ph
            for lbl in [getattr(self, n, None) for n in
                        ("main_bat_lbl",)]:
                if lbl:
                    lbl.config(image=ph)
                    lbl.image = ph
        else:
            txt = f"{'⚡' if self.charging else '🔋'} {self.battery_pct}%"
            for lbl in [getattr(self, n, None) for n in ("main_bat_lbl",)]:
                if lbl:
                    lbl.config(text=txt, image="")

        # Splash battery (bigger icon)
        if self.screen == "splash":
            self._refresh_splash_bat()

    def _refresh_warn_labels(self):
        warn = "" if self.stm_ok else "⚠  Контроллер отключён"
        for attr in ("splash_warn", "task_warn", "main_warn", "finish_warn"):
            lbl = getattr(self, attr, None)
            if lbl:
                lbl.config(text=warn)

    # ─────────────────────────────────────────────────────────────────────────
    # SCREEN SWITCHING
    # ─────────────────────────────────────────────────────────────────────────

    def _show(self, name: str):
        for f in self.frames.values():
            f.place_forget()
        self.screen = name
        f = self.frames[name]
        f.place(x=0, y=0, width=W, height=H)
        f.lift()

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: SPLASH / STANDBY
    # ─────────────────────────────────────────────────────────────────────────

    def _build_splash(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["splash"] = f

        self.splash_logo_lbl = tk.Label(f, bg=BG)
        self.splash_logo_lbl.place(relx=0.5, rely=0.33, anchor="center")

        self.splash_bat_lbl = tk.Label(f, bg=BG)
        self.splash_bat_lbl.place(relx=0.5, rely=0.72, anchor="center")

        self.splash_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG, font=("DejaVu Sans", 13))
        self.splash_warn.place(relx=0.5, rely=0.90, anchor="center")

    def _show_splash(self):
        self._show("splash")

        # Logo — ещё больше
        logo_ph = load_asset("logo.png", size=(580, 330))   # было (420, 240)
        if logo_ph:
            self._photo_cache["logo"] = logo_ph
            self.splash_logo_lbl.config(image=logo_ph, text="")
            self.splash_logo_lbl.image = logo_ph
        else:
            self.splash_logo_lbl.config(
                text="HyperspectRus",
                font=("DejaVu Sans", 44, "bold"),
                fg=ACCENT_GRN, image="",
            )

        self._refresh_splash_bat()
        self._refresh_warn_labels()

    def _refresh_splash_bat(self):
        icon_name = battery_icon_name(self.battery_pct, self.charging)
        ph = load_asset(icon_name, size=(320, 160))   # было (200, 100)
        if ph:
            self._photo_cache["splash_bat"] = ph
            self.splash_bat_lbl.config(image=ph, text="")
            self.splash_bat_lbl.image = ph
        else:
            sym = "⚡" if self.charging else "🔋"
            self.splash_bat_lbl.config(
                text=f"{sym}  {self.battery_pct}%",
                font=("DejaVu Sans", 32), fg=TEXT_WHITE, image="",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: TASK SELECT
    # ─────────────────────────────────────────────────────────────────────────

    def _build_task_select(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["task_select"] = f

        self.task_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG, font=("DejaVu Sans", 12))
        self.task_warn.place(x=10, y=6)

        # Card
        card = tk.Frame(f, bg=CARD_BG, bd=0)
        card.place(relx=0.5, rely=0.33, anchor="center", width=720, height=210)

        self.task_title = tk.Label(card,
                                    text=f'ID пациента:  "{PATIENT_ID}"',
                                    font=("DejaVu Sans", 34, "bold"),
                                    bg=CARD_BG, fg=CARD_FG)
        self.task_title.place(relx=0.5, rely=0.30, anchor="center")

        self.task_action = tk.Label(card,
                                     text="Начать съёмку?",
                                     font=("DejaVu Sans", 28),
                                     bg=CARD_BG, fg=CARD_FG)
        self.task_action.place(relx=0.5, rely=0.68, anchor="center")

        # X button
        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 52, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._task_cancel,
                  ).place(relx=0.63, rely=0.76, anchor="center", width=140, height=120)

        # ✓ button
        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 52, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._task_confirm,
                  ).place(relx=0.83, rely=0.76, anchor="center", width=140, height=120)

    def _show_task_select(self, action_text="Начать съёмку?"):
        self.task_action.config(text=action_text)
        self._show("task_select")
        self._refresh_warn_labels()

    def _task_cancel(self):
        pass   # intentionally do nothing

    def _task_confirm(self):
        self._show_main()

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: MAIN SHOOTING SCREEN
    # ─────────────────────────────────────────────────────────────────────────

    def _build_main(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["main"] = f

        # ── Status bar ────────────────────────────────────────────────────────
        sb = tk.Frame(f, bg=STATUS_BG, height=STATUS_H)
        sb.place(x=0, y=0, width=W, height=STATUS_H)
        sb.pack_propagate(False)

        tk.Label(sb, text="HyperspectRus",
                 font=("DejaVu Sans", 13, "bold"),
                 fg=ACCENT_GRN, bg=STATUS_BG,
                 ).place(x=8, y=3)

        self.main_warn = tk.Label(sb, text="", fg=WARN_COL, bg=STATUS_BG,
                                   font=("DejaVu Sans", 10))
        self.main_warn.place(x=200, y=3)

        self.main_bat_lbl = tk.Label(sb, text="", fg=TEXT_WHITE, bg=STATUS_BG,
                                      font=("DejaVu Sans", 10))
        self.main_bat_lbl.place(x=W - 105, y=1)

        # ── Camera preview ────────────────────────────────────────────────────
        self.main_prev_lbl = tk.Label(f, bg="#0A0A0A",
                                       text="Камера недоступна",
                                       fg=TEXT_DIM,
                                       font=("DejaVu Sans", 16))
        self.main_prev_lbl.place(x=0, y=STATUS_H,
                                  width=PREVIEW_W, height=H - STATUS_H)

        # ── Right panel ───────────────────────────────────────────────────────
        BY  = STATUS_H + 10         # panel top
        BH  = 90                    # button height
        GAP = 12

        self.btn_rgb = tk.Button(f, text="RGB",
                                  font=("DejaVu Sans", 26, "bold"),
                                  bg=BTN_ACTIVE, fg="#111111",
                                  activebackground=BTN_ACTIVE, activeforeground="#111111",
                                  relief="flat", bd=0, highlightthickness=0,
                                  cursor="hand2",
                                  command=lambda: self._set_mode(M_RGB))
        self.btn_rgb.place(x=PANEL_X, y=BY, width=PANEL_W, height=BH)

        self.btn_ir  = tk.Button(f, text="ИК",
                                  font=("DejaVu Sans", 26, "bold"),
                                  bg=BTN_IDLE, fg="#111111",
                                  activebackground=BTN_IDLE, activeforeground="#111111",
                                  relief="flat", bd=0, highlightthickness=0,
                                  cursor="hand2",
                                  command=lambda: self._set_mode(M_IR))
        self.btn_ir.place(x=PANEL_X, y=BY + BH + GAP, width=PANEL_W, height=BH)

        self.btn_nbi = tk.Button(f, text="NBI",
                                  font=("DejaVu Sans", 26, "bold"),
                                  bg=BTN_IDLE, fg="#111111",
                                  activebackground=BTN_IDLE, activeforeground="#111111",
                                  relief="flat", bd=0, highlightthickness=0,
                                  cursor="hand2",
                                  command=lambda: self._set_mode(M_NBI))
        self.btn_nbi.place(x=PANEL_X, y=BY + 2*(BH + GAP), width=PANEL_W, height=BH)

        # Finish button — taller, at bottom
        self.btn_finish = tk.Button(f, text="завершить",
                                     font=("DejaVu Sans", 20, "bold"),
                                     bg=BTN_FINISH, fg="#DDDDDD",
                                     activebackground=BTN_FINISH, activeforeground="#DDDDDD",
                                     relief="flat", bd=0, highlightthickness=0,
                                     cursor="hand2",
                                     command=self._show_finish_confirm)
        self.btn_finish.place(x=PANEL_X - 4, y=H - 115,
                               width=PANEL_W + 8, height=106)

    def _show_main(self):
        self._show("main")
        self._refresh_warn_labels()
        self._refresh_battery_display()
        self._set_mode(self.led_mode)   # always turn on LEDs immediately
        self._start_cam_preview()

    def _set_mode(self, mode: str):
        self.led_mode = mode
        for btn, m in [(self.btn_rgb, M_RGB), (self.btn_ir, M_IR), (self.btn_nbi, M_NBI)]:
            c = BTN_ACTIVE if mode == m else BTN_IDLE
            btn.config(bg=c, activebackground=c)
        if self.stm.connected:
            threading.Thread(
                target=self.stm.set_preview_leds,
                args=(mode, PREVIEW_DUTY),
                daemon=True,
            ).start()

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: FINISH CONFIRM
    # ─────────────────────────────────────────────────────────────────────────

    def _build_finish_confirm(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["finish_confirm"] = f

        self.finish_warn = tk.Label(f, text="", fg=WARN_COL, bg=BG,
                                     font=("DejaVu Sans", 12))
        self.finish_warn.place(x=10, y=6)

        card = tk.Frame(f, bg=CARD_BG, bd=0)
        card.place(relx=0.5, rely=0.33, anchor="center", width=720, height=210)

        tk.Label(card,
                 text=f'ID пациента:  "{PATIENT_ID}"',
                 font=("DejaVu Sans", 34, "bold"),
                 bg=CARD_BG, fg=CARD_FG,
                 ).place(relx=0.5, rely=0.30, anchor="center")

        tk.Label(card,
                 text="Завершить съёмку?",
                 font=("DejaVu Sans", 28),
                 bg=CARD_BG, fg=CARD_FG,
                 ).place(relx=0.5, rely=0.68, anchor="center")

        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 52, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._finish_cancel,
                  ).place(relx=0.63, rely=0.76, anchor="center", width=140, height=120)

        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 52, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._finish_ok,
                  ).place(relx=0.83, rely=0.76, anchor="center", width=140, height=120)

    def _show_finish_confirm(self):
        self._show("finish_confirm")
        self._refresh_warn_labels()

    def _finish_cancel(self):
        self._show_main()

    def _finish_ok(self):
        self._stop_cam_preview()
        threading.Thread(target=self.stm.all_off, daemon=True).start()
        self._show_task_select()

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: CAPTURING
    # ─────────────────────────────────────────────────────────────────────────

    def _build_capturing(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["capturing"] = f

        tk.Label(f, text="Идёт съёмка…",
                 font=("DejaVu Sans", 46, "bold"),
                 fg=ACCENT_GRN, bg=BG,
                 ).place(relx=0.5, rely=0.38, anchor="center")

        self.cap_progress = tk.Label(f, text="",
                                      font=("DejaVu Sans", 22),
                                      fg=TEXT_DIM, bg=BG)
        self.cap_progress.place(relx=0.5, rely=0.60, anchor="center")

        self.cap_led_lbl = tk.Label(f, text="",
                                     font=("DejaVu Sans", 18),
                                     fg="#7B9A2A", bg=BG)
        self.cap_led_lbl.place(relx=0.5, rely=0.72, anchor="center")

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
        images = []

        if CAM_OK and self.cam:
            try:
                # Используем уже настроенную конфигурацию — ничего не переключаем
                self.cam.start()
                self.cam_running = True
                time.sleep(0.4)

                for i, (led, wl, duty) in enumerate(LED_TABLE):
                    msg = f"Снимок {i+1} / {len(LED_TABLE)}  —  {wl} нм"
                    self.root.after(0, lambda m=msg: self.cap_progress.config(text=m))
                    lmsg = f"LED {led}  ·  {duty}% PWM"
                    self.root.after(0, lambda m=lmsg: self.cap_led_lbl.config(text=m))

                    if self.stm.connected:
                        self.stm.led_duty(led, duty)
                        self.stm.led_on(led)
                        time.sleep(0.03)

                    

                    # Захват
                    req = self.cam.capture_request()
                    buf = io.BytesIO()
                    req.save("main", buf, format="jpeg")
                    req.release()

                    if self.stm.connected:
                        self.stm.led_off(led)

                    buf.seek(0)
                    img = Image.open(buf)
                    img.load()
                    images.append((wl, img.copy()))

                self.cam.stop()
                self.cam_running = False

            except Exception as e:
                print(f"Capture error: {e}")
                images = self._dummy_images()
        else:
            images = self._dummy_images()
        self.captures  = images
        self.prev_idx  = 0
        self.root.after(0, self._show_preview_screen)

    def _dummy_images(self):
        """Synthetic images for testing without camera."""
        imgs = []
        colors = [
            (30, 60, 200),   # 450 blue
            (20, 180, 60),   # 520 green
            (200, 40, 40),   # 670 red
            (140, 60, 140),  # 780
            (150, 70, 110),  # 800
            (130, 80, 90),   # 850
            (110, 90, 70),   # 890
            (90, 100, 50),   # 940
        ]
        for (led, wl, duty), col in zip(LED_TABLE, colors):
            img = Image.new("RGB", (640, 480), color=col)
            if PIL_OK:
                draw = ImageDraw.Draw(img)
                draw.text((20, 20), f"{wl} nm  LED{led}", fill=(255, 255, 255))
            imgs.append((wl, img))
            time.sleep(0.25)
        return imgs

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD: PHOTO PREVIEW
    # ─────────────────────────────────────────────────────────────────────────

    def _build_preview(self):
        f = tk.Frame(self.root, bg=BG)
        self.frames["preview"] = f

        # Photo display
        self.prev_photo_lbl = tk.Label(f, bg="#0A0A0A",
                                        text="",
                                        fg=TEXT_DIM,
                                        font=("DejaVu Sans", 14))
        self.prev_photo_lbl.place(x=0, y=0, width=PREVIEW_W, height=H)

        # ── Right panel ────────────────────────────────────────────────────
        px, pw = PANEL_X, PANEL_W
        HALF   = (pw - 8) // 2

        # Wavelength
        self.prev_wl_lbl = tk.Label(f, text="450 нм",
                                     font=("DejaVu Sans", 26, "bold"),
                                     fg=TEXT_WHITE, bg=BG)
        self.prev_wl_lbl.place(x=px, y=10, width=pw, height=44)

        self.prev_cnt_lbl = tk.Label(f, text="1 / 8",
                                      font=("DejaVu Sans", 16),
                                      fg=TEXT_DIM, bg=BG)
        self.prev_cnt_lbl.place(x=px, y=56, width=pw, height=28)

        # Navigation row  ◀  ▶
        tk.Button(f, text="◀",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_left,
                  ).place(x=px, y=96, width=HALF, height=100)

        tk.Button(f, text="▶",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_right,
                  ).place(x=px + HALF + 8, y=96, width=HALF, height=100)

        # Brightness row  −  +
        tk.Button(f, text="−",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_left,   # repurposed for nav in test version
                  ).place(x=px, y=208, width=HALF, height=100)

        tk.Button(f, text="+",
                  font=("DejaVu Sans", 34, "bold"),
                  bg=BTN_IDLE, fg="#333333",
                  activebackground=BTN_IDLE, activeforeground="#333333",
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_right,  # repurposed for nav in test version
                  ).place(x=px + HALF + 8, y=208, width=HALF, height=100)

        # Delete / Save row
        tk.Button(f, text="✗",
                  font=("DejaVu Sans", 46, "bold"),
                  bg=BTN_IDLE, fg=BTN_DANGER,
                  activebackground=BTN_IDLE, activeforeground=BTN_DANGER,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_discard,
                  ).place(x=px, y=322, width=HALF, height=140)

        tk.Button(f, text="✓",
                  font=("DejaVu Sans", 46, "bold"),
                  bg=BTN_IDLE, fg=BTN_OK,
                  activebackground=BTN_IDLE, activeforeground=BTN_OK,
                  relief="flat", bd=0, highlightthickness=0,
                  cursor="hand2",
                  command=self._prev_save,
                  ).place(x=px + HALF + 8, y=322, width=HALF, height=140)

    def _show_preview_screen(self):
        self._show("preview")
        self._refresh_preview_image()

    def _refresh_preview_image(self):
        if not self.captures:
            return
        wl, img = self.captures[self.prev_idx]

        cropped = self._resize_to_fill(img, PREVIEW_W, H)
        ph = ImageTk.PhotoImage(cropped)
        
        self._photo_cache["prev_img"] = ph
        self.prev_photo_lbl.config(image=ph)
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
        self._return_to_main()

    def _prev_save(self):
        # Test version: discard as well (no real saving)
        self._return_to_main()

    def _return_to_main(self):
        self.captures = []
        self._show_main()   # здесь уже вызывается _start_cam_preview

    # ─────────────────────────────────────────────────────────────────────────
    # BOOT
    # ─────────────────────────────────────────────────────────────────────────

    def _boot(self):
        """Decide first screen based on charging state."""
        if self.charging in (1, 2):
            self._show_splash()
        else:
            self._show_splash()
            # Wait 1.5 s on splash, then go to task select
            self.root.after(1500, self._show_task_select)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App()
