#!/usr/bin/env python3
"""
HyperspectRus — Hyperspectral Camera Application
Raspberry Pi Zero + PiCamera2 + STM32 via USB Serial
Screen: 800x480 touchscreen
"""

import os
import sys
import time
import io
import threading
import datetime
import signal

# Ensure DISPLAY is set
if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":0"

import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageDraw, ImageFont
import serial

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
SCREEN_W, SCREEN_H = 800, 480
PATIENT_ID = "022"
STM_PORT   = "/dev/ttyACM0"
STM_BAUD   = 115200

# LED table: (index, wavelength_nm, capture_duty%)
LED_TABLE = [
    (1, 450,  100),
    (2, 517,  100),
    (3, 671,   60),
    (4, 775,   60),
    (5, 803,   50),
    (6, 851,   40),
    (7, 888,   60),
    (8, 939,  100),
]

# LED groups for preview illumination (40% PWM)
LED_GROUPS = {
    "RGB": [1, 2, 3],
    "ИК":  [8],
    "NBI": [2, 3],
}
PREVIEW_DUTY = 40
DEFAULT_GROUP = "RGB"

# Battery voltage range
BAT_MIN_MV = 3300
BAT_MAX_MV = 4200

# Colors
BG       = "#000000"
ACCENT   = "#8BC34A"   # olive-green
ACCENT2  = "#29B6F6"   # cyan
RED_BTN  = "#C62828"
GREEN_BTN= "#558B2F"
GRAY_BTN = "#424242"
TEXT_W   = "#FFFFFF"
TEXT_D   = "#CCCCCC"
PANEL_BG = "#111111"
CARD_BG  = "#1A1A1A"
DISABLED = "#333333"

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")


# ─────────────────────────────────────────
#  STM32 CONTROLLER (with reconnect logic)
# ─────────────────────────────────────────
class STMController:
    def __init__(self, port=STM_PORT, baud=STM_BAUD):
        self.port = port
        self.baud = baud
        self.ser = None
        self.connected = False
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            time.sleep(0.5)
            self.connected = True
        except Exception as e:
            print(f"[STM] Connect error: {e}")
            self.ser = None
            self.connected = False

    def _send(self, cmd: str) -> str:
        if not self.connected or self.ser is None:
            return "disconnected"
        try:
            with self._lock:
                self.ser.write(cmd.encode())
                self.ser.flush()
                time.sleep(0.005)
                resp = self.ser.readline().decode(errors="ignore").strip()
                return resp
        except Exception as e:
            print(f"[STM] Send error: {e}")
            self.connected = False
            self._try_reconnect()
            return "error"

    def _try_reconnect(self):
        print("[STM] Reconnecting…")
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        time.sleep(1)
        self._connect()

    def reset(self):
        self._send("resetDevice\n")
        time.sleep(2)
        self._try_reconnect()

    def get_charging_state(self) -> int:
        r = self._send("getChargingState\n")
        try:
            return int(r)
        except Exception:
            return -1

    def get_battery_mv(self) -> int:
        r = self._send("getBatteryVoltage\n")
        try:
            return int(r)
        except Exception:
            return -1

    def led_duty(self, n: int, duty: int):
        cmd = f"sld{n}{duty}\n"
        self._send(cmd)

    def led_on(self, n: int):
        self._send(f"so{n}\n")

    def led_off(self, n: int):
        self._send(f"sso{n}\n")

    def all_leds_off(self):
        for i in range(1, 9):
            self.led_off(i)

    def set_group(self, group_name: str, duty: int = PREVIEW_DUTY):
        self.all_leds_off()
        leds = LED_GROUPS.get(group_name, [])
        for n in leds:
            self.led_duty(n, duty)
            self.led_on(n)

    def close(self):
        try:
            self.all_leds_off()
            if self.ser:
                self.ser.close()
        except Exception:
            pass


# ─────────────────────────────────────────
#  CAMERA (with graceful fallback)
# ─────────────────────────────────────────
class CameraController:
    def __init__(self):
        self.cam = None
        self.available = False
        self._init_camera()

    def _init_camera(self):
        try:
            from picamera2 import Picamera2
            from libcamera import controls as lc
            self._lc = lc
            self.cam = Picamera2()
            cfg = self.cam.create_preview_configuration(
                main={"size": (640, 480)}
            )
            self.cam.configure(cfg)
            self.cam.start()
            self._apply_controls(preview=True)
            self.available = True
        except Exception as e:
            print(f"[CAM] Init error: {e}")
            self.available = False

    def _apply_controls(self, preview=True):
        if not self.available:
            return
        try:
            if preview:
                ctrl = {
                    "AeEnable": True,
                    "AwbEnable": True,
                }
            else:
                ctrl = {
                    "AeEnable": False,
                    "AwbEnable": False,
                    "ExposureTime": 1000,
                    "AnalogueGain": 1.1,
                    "ColourGains": (1.0, 1.0),
                }
            from libcamera import controls as lc
            ctrl["AfMode"] = lc.AfModeEnum.Manual
            ctrl["LensPosition"] = 12.0
            self.cam.set_controls(ctrl)
        except Exception as e:
            print(f"[CAM] Controls error: {e}")

    def capture_frame_pil(self) -> Image.Image | None:
        """Grab one frame as PIL Image."""
        if not self.available:
            return None
        try:
            buf = io.BytesIO()
            req = self.cam.capture_request()
            req.save("main", buf, format="jpeg")
            req.release()
            buf.seek(0)
            return Image.open(buf).copy()
        except Exception as e:
            print(f"[CAM] Capture error: {e}")
            return None

    def capture_sequence(self) -> list[tuple[int, bytes]]:
        """Capture 8 images (wavelength, jpeg_bytes). LEDs managed externally."""
        images = []
        if not self.available:
            return images
        self._apply_controls(preview=False)
        time.sleep(0.3)
        # discard 3 frames
        for _ in range(3):
            try:
                req = self.cam.capture_request()
                req.release()
            except Exception:
                pass
        return images  # filled by caller

    def stop(self):
        if self.cam:
            try:
                self.cam.stop()
            except Exception:
                pass


# ─────────────────────────────────────────
#  ASSET HELPERS
# ─────────────────────────────────────────
def load_image(path: str, size=None) -> Image.Image | None:
    try:
        img = Image.open(path).convert("RGBA")
        if size:
            img = img.resize(size, Image.LANCZOS)
        return img
    except Exception as e:
        print(f"[ASSET] Cannot load {path}: {e}")
        return None


def pil_to_tk(img: Image.Image) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(img)


def make_bat_icon(level: int, charging: bool, size=(60, 30)) -> Image.Image:
    """Generate battery icon programmatically."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body_w = w - 6
    # Body
    d.rounded_rectangle([0, 4, body_w, h - 4], radius=3, outline="#FFFFFF", width=2)
    # Terminal
    d.rectangle([body_w, h // 2 - 4, w - 1, h // 2 + 4], fill="#FFFFFF")
    # Fill
    fill_w = int((body_w - 4) * level / 100)
    color = "#4CAF50" if level > 20 else "#F44336"
    if fill_w > 0:
        d.rectangle([2, 6, 2 + fill_w, h - 6], fill=color)
    # Lightning bolt for charging
    if charging:
        bx, by = body_w // 2 - 4, 3
        pts = [(bx+4, by), (bx+1, by+10), (bx+5, by+10), (bx+2, by+20), (bx+9, by+7), (bx+5, by+7), (bx+8, by)]
        d.polygon(pts, fill="#FFD600")
    return img


def bat_level_pct(mv: int) -> int:
    pct = (mv - BAT_MIN_MV) / (BAT_MAX_MV - BAT_MIN_MV) * 100
    return max(0, min(100, int(pct)))


# ─────────────────────────────────────────
#  ROUNDED BUTTON HELPER
# ─────────────────────────────────────────
def make_round_btn(canvas: tk.Canvas, x, y, w, h, text, bg, fg,
                   font_obj, radius=18, command=None, tag=None):
    """Draw a rounded-rectangle button on a canvas."""
    items = []

    def draw_rrect(fill, outline):
        r = radius
        pts = [
            x+r, y,
            x+w-r, y,
            x+w, y+r,
            x+w, y+h-r,
            x+w-r, y+h,
            x+r, y+h,
            x, y+h-r,
            x, y+r,
        ]
        return canvas.create_polygon(pts, smooth=True, fill=fill,
                                     outline=outline, width=2)

    body = draw_rrect(bg, bg)
    lbl  = canvas.create_text(x + w//2, y + h//2, text=text,
                               fill=fg, font=font_obj)
    items = [body, lbl]

    if tag:
        for it in items:
            canvas.addtag_withtag(tag, it)

    if command:
        for it in items:
            canvas.tag_bind(it, "<Button-1>", lambda e: command())

    return items


# ─────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────
class HyperspectRus(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HyperspectRus")
        self.configure(bg=BG)
        self.geometry(f"{SCREEN_W}x{SCREEN_H}+0+0")
        self.resizable(False, False)
        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: None)  # disable escape

        # State
        self.patient_id   = PATIENT_ID
        self.light_group  = tk.StringVar(value=DEFAULT_GROUP)
        self.current_frame = None   # PIL image for preview
        self.captured_images: list[tuple[int, bytes]] = []
        self.preview_idx  = 0
        self.stm_ok       = True

        # Init hardware (non-blocking)
        self.stm  = None
        self.cam  = None
        self._hw_init_done = False

        # Fonts
        self._load_fonts()

        # Build UI frames
        self._frames: dict[str, tk.Frame] = {}
        self._build_all_screens()

        # Show splash
        self._show_screen("splash")

        # Start hardware init in background
        threading.Thread(target=self._init_hardware, daemon=True).start()

        # Battery polling
        self._bat_level = 75
        self._bat_charging = False
        self._poll_battery()

        # External GPIO button
        self._setup_gpio()

        # Preview loop
        self._preview_running = False
        self._preview_after   = None

        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ── Fonts ──────────────────────────────
    def _load_fonts(self):
        self.fn_big    = tkfont.Font(family="DejaVu Sans", size=36, weight="bold")
        self.fn_med    = tkfont.Font(family="DejaVu Sans", size=24)
        self.fn_small  = tkfont.Font(family="DejaVu Sans", size=16)
        self.fn_title  = tkfont.Font(family="DejaVu Sans", size=20, weight="bold")
        self.fn_tiny   = tkfont.Font(family="DejaVu Sans", size=12)

    # ── Hardware init ───────────────────────
    def _init_hardware(self):
        try:
            self.stm = STMController()
        except Exception as e:
            print(f"[APP] STM init failed: {e}")
            self.stm = None

        try:
            self.cam = CameraController()
        except Exception as e:
            print(f"[APP] Camera init failed: {e}")
            self.cam = None

        self._hw_init_done = True

        # After 2 sec on splash, check charging state
        self.after(2000, self._after_splash)

    def _after_splash(self):
        if not self._hw_init_done:
            self.after(500, self._after_splash)
            return
        self._check_charging_and_route()

    def _check_charging_and_route(self):
        charging = self._get_charging()
        if charging:
            self._show_screen("standby")
        else:
            self._show_screen("task")
        # Start polling charging state
        self._poll_charging()

    def _get_charging(self) -> bool:
        if self.stm and self.stm.connected:
            st = self.stm.get_charging_state()
            return st > 0
        return False

    def _poll_charging(self):
        charging = self._get_charging()
        current = self._current_screen()
        if charging and current not in ("standby", "splash"):
            self._preview_stop()
            if self.stm:
                self.stm.all_leds_off()
            self._show_screen("standby")
        elif not charging and current == "standby":
            self._show_screen("task")
        self.after(3000, self._poll_charging)

    def _poll_battery(self):
        def _do():
            if self.stm and self.stm.connected:
                mv = self.stm.get_battery_mv()
                if mv > 0:
                    self._bat_level = bat_level_pct(mv)
                cs = self.stm.get_charging_state()
                self._bat_charging = cs > 0
                self.stm_ok = True
            elif self.stm:
                self.stm_ok = False
            else:
                self.stm_ok = False
            self._update_status_bar()
            self.after(5000, self._poll_battery)
        _do()

    def _update_status_bar(self):
        """Refresh battery icon and STM warning on all screens."""
        for name, frame in self._frames.items():
            bar = getattr(frame, "_status_bar", None)
            if bar:
                bar.update_status(self._bat_level, self._bat_charging,
                                  self.stm_ok if self.stm else False)

    # ── GPIO ───────────────────────────────
    def _setup_gpio(self):
        try:
            from gpiozero import Button as GpioButton
            self._gpio_btn = GpioButton(26, pull_up=True, bounce_time=0.05)
            self._gpio_btn.when_pressed = self._on_hw_button
        except Exception as e:
            print(f"[GPIO] Not available: {e}")
            self._gpio_btn = None

    def _on_hw_button(self):
        if self._current_screen() == "main":
            self.after(0, self._start_capture)

    # ── Screen management ───────────────────
    def _current_screen(self) -> str:
        for name, frame in self._frames.items():
            if frame.winfo_ismapped():
                return name
        return ""

    def _show_screen(self, name: str):
        for n, f in self._frames.items():
            f.pack_forget()
        if name in self._frames:
            self._frames[name].pack(fill="both", expand=True)
            self._frames[name].event_generate("<<ScreenShown>>")

    # ── Build all screens ────────────────────
    def _build_all_screens(self):
        self._frames["splash"]  = self._build_splash()
        self._frames["standby"] = self._build_standby()
        self._frames["task"]    = self._build_task()
        self._frames["main"]    = self._build_main()
        self._frames["confirm_end"] = self._build_confirm_end()
        self._frames["capturing"]   = self._build_capturing()
        self._frames["preview_photos"] = self._build_preview_photos()

    # ─────────────────────────────────────────
    #  SPLASH SCREEN
    # ─────────────────────────────────────────
    def _build_splash(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        tk.Label(f, text="HyperspectRus", font=self.fn_big,
                 bg=BG, fg=ACCENT).pack(expand=True)
        tk.Label(f, text="Hyperspectral Imaging System",
                 font=self.fn_small, bg=BG, fg="#666666").pack(pady=(0, 40))
        # Loading bar
        self._splash_bar = tk.Canvas(f, width=300, height=6,
                                     bg="#222222", highlightthickness=0)
        self._splash_bar.pack(pady=10)
        self._splash_bar.create_rectangle(0, 0, 0, 6, fill=ACCENT, tags="bar")
        f._status_bar = None
        self._animate_splash_bar()
        return f

    def _animate_splash_bar(self, val=0):
        if not hasattr(self, "_splash_bar"):
            return
        self._splash_bar.coords("bar", 0, 0, val, 6)
        if val < 300:
            self.after(10, lambda: self._animate_splash_bar(val + 3))

    # ─────────────────────────────────────────
    #  STANDBY SCREEN (charging)
    # ─────────────────────────────────────────
    def _build_standby(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        f._status_bar = None
        tk.Label(f, text="HyperspectRus", font=self.fn_big,
                 bg=BG, fg=ACCENT).pack(expand=True, side="top", pady=(80, 0))
        # Battery icon (large, ~200px wide)
        self._standby_bat_canvas = tk.Canvas(f, width=200, height=100,
                                              bg=BG, highlightthickness=0)
        self._standby_bat_canvas.pack(pady=20)
        self._standby_bat_img_ref = None
        self._update_standby_bat()

        tk.Label(f, text="Зарядка…", font=self.fn_med,
                 bg=BG, fg="#888888").pack(pady=(0, 80))
        f.bind("<<ScreenShown>>", lambda e: self._update_standby_bat())
        return f

    def _update_standby_bat(self):
        img = make_bat_icon(self._bat_level, True, size=(200, 100))
        tk_img = pil_to_tk(img)
        self._standby_bat_canvas.delete("all")
        self._standby_bat_canvas.create_image(0, 0, anchor="nw", image=tk_img)
        self._standby_bat_img_ref = tk_img
        self.after(3000, self._update_standby_bat)

    # ─────────────────────────────────────────
    #  TASK SELECTION SCREEN
    # ─────────────────────────────────────────
    def _build_task(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        f._status_bar = StatusBar(f)
        f._status_bar.pack(fill="x", side="top")

        center = tk.Frame(f, bg=BG)
        center.pack(expand=True)

        # Card
        card = tk.Frame(center, bg=CARD_BG, padx=40, pady=30)
        card.pack(padx=60, pady=20)
        card.configure(bd=0, highlightbackground="#333", highlightthickness=1)

        tk.Label(card, text=f'ID пациента: "{self.patient_id}"',
                 font=self.fn_big, bg=CARD_BG, fg=TEXT_W).pack()
        tk.Label(card, text="Начать съёмку?",
                 font=self.fn_big, bg=CARD_BG, fg=TEXT_D).pack(pady=(20, 0))

        # Buttons
        btn_row = tk.Frame(center, bg=BG)
        btn_row.pack(pady=20)

        btn_x = RoundButton(btn_row, text="✕", bg=RED_BTN, fg=TEXT_W,
                            font=self.fn_big, width=100, height=80,
                            command=lambda: None)
        btn_x.pack(side="left", padx=20)

        btn_ok = RoundButton(btn_row, text="✓", bg=GREEN_BTN, fg=TEXT_W,
                             font=self.fn_big, width=100, height=80,
                             command=self._go_main)
        btn_ok.pack(side="left", padx=20)

        f._status_bar.update_status(75, False, True)
        return f

    def _go_main(self):
        self._show_screen("main")
        self._preview_start()
        if self.stm:
            self.stm.set_group(self.light_group.get(), PREVIEW_DUTY)

    # ─────────────────────────────────────────
    #  MAIN SHOOTING SCREEN
    # ─────────────────────────────────────────
    def _build_main(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)

        # Top status bar
        f._status_bar = StatusBar(f)
        f._status_bar.pack(fill="x", side="top")

        # Title strip
        title_bar = tk.Frame(f, bg=PANEL_BG, height=36)
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="HyperspectRus", font=self.fn_title,
                 bg=PANEL_BG, fg=ACCENT).pack(side="left", padx=16)
        tk.Label(title_bar, text=f"ID: {self.patient_id}",
                 font=self.fn_small, bg=PANEL_BG, fg="#888").pack(side="right", padx=16)

        # Body
        body = tk.Frame(f, bg=BG)
        body.pack(fill="both", expand=True)

        # Camera preview (left)
        self._preview_canvas = tk.Canvas(body, width=560, height=360,
                                          bg="#0a0a0a", highlightthickness=0)
        self._preview_canvas.pack(side="left", padx=(10, 5), pady=10)
        self._preview_img_ref = None

        # Right panel
        right = tk.Frame(body, bg=BG, width=210)
        right.pack(side="right", fill="y", padx=(5, 10), pady=10)
        right.pack_propagate(False)

        # Light mode buttons
        for grp_name, color in [("RGB", ACCENT), ("ИК", ACCENT2), ("NBI", "#FF8A65")]:
            b = RoundButton(right, text=grp_name,
                            bg=color if self.light_group.get() == grp_name else GRAY_BTN,
                            fg=TEXT_W, font=self.fn_med,
                            width=190, height=62,
                            command=lambda g=grp_name: self._select_group(g))
            b.pack(pady=6)
            setattr(f, f"_btn_{grp_name}", b)

        tk.Frame(right, bg=BG, height=10).pack()

        # Finish button
        btn_end = RoundButton(right, text="завершить",
                               bg="#37474F", fg=TEXT_D,
                               font=self.fn_med, width=190, height=62,
                               command=self._ask_finish)
        btn_end.pack(side="bottom", pady=6)

        f.bind("<<ScreenShown>>", lambda e: self._preview_start())
        return f

    def _select_group(self, grp):
        self.light_group.set(grp)
        f = self._frames["main"]
        for g, color in [("RGB", ACCENT), ("ИК", ACCENT2), ("NBI", "#FF8A65")]:
            btn = getattr(f, f"_btn_{g}", None)
            if btn:
                btn.configure(bg=color if g == grp else GRAY_BTN)
        if self.stm and self._current_screen() == "main":
            self.stm.set_group(grp, PREVIEW_DUTY)

    # ─────────────────────────────────────────
    #  PREVIEW LOOP
    # ─────────────────────────────────────────
    def _preview_start(self):
        self._preview_running = True
        self._preview_tick()

    def _preview_stop(self):
        self._preview_running = False
        if self._preview_after:
            self.after_cancel(self._preview_after)
            self._preview_after = None

    def _preview_tick(self):
        if not self._preview_running:
            return
        if self.cam and self.cam.available:
            img = self.cam.capture_frame_pil()
            if img:
                img = img.resize((560, 360), Image.LANCZOS)
                tk_img = pil_to_tk(img)
                self._preview_canvas.delete("all")
                self._preview_canvas.create_image(0, 0, anchor="nw", image=tk_img)
                self._preview_img_ref = tk_img
        else:
            # Placeholder
            self._preview_canvas.delete("all")
            self._preview_canvas.create_text(280, 180, text="Камера недоступна",
                                              fill="#555", font=self.fn_med)
        self._preview_after = self.after(100, self._preview_tick)

    # ─────────────────────────────────────────
    #  CONFIRM END
    # ─────────────────────────────────────────
    def _build_confirm_end(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        f._status_bar = StatusBar(f)
        f._status_bar.pack(fill="x", side="top")

        center = tk.Frame(f, bg=BG)
        center.pack(expand=True)

        card = tk.Frame(center, bg=CARD_BG, padx=40, pady=30)
        card.pack(padx=60, pady=20)

        tk.Label(card, text=f'ID пациента: "{self.patient_id}"',
                 font=self.fn_big, bg=CARD_BG, fg=TEXT_W).pack()
        tk.Label(card, text="Завершить съёмку?",
                 font=self.fn_big, bg=CARD_BG, fg=TEXT_D).pack(pady=(20, 0))

        btn_row = tk.Frame(center, bg=BG)
        btn_row.pack(pady=20)

        RoundButton(btn_row, text="✕", bg=RED_BTN, fg=TEXT_W,
                    font=self.fn_big, width=100, height=80,
                    command=self._back_to_main).pack(side="left", padx=20)

        RoundButton(btn_row, text="✓", bg=GREEN_BTN, fg=TEXT_W,
                    font=self.fn_big, width=100, height=80,
                    command=self._go_task).pack(side="left", padx=20)

        return f

    def _ask_finish(self):
        self._preview_stop()
        if self.stm:
            self.stm.all_leds_off()
        self._show_screen("confirm_end")

    def _back_to_main(self):
        self._show_screen("main")
        self._preview_start()
        if self.stm:
            self.stm.set_group(self.light_group.get(), PREVIEW_DUTY)

    def _go_task(self):
        self._show_screen("task")

    # ─────────────────────────────────────────
    #  CAPTURING SCREEN
    # ─────────────────────────────────────────
    def _build_capturing(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        f._status_bar = None
        tk.Label(f, text="⏺", font=tkfont.Font(size=64),
                 bg=BG, fg=RED_BTN).pack(expand=True, pady=(60, 0))
        tk.Label(f, text="Идёт съёмка…", font=self.fn_big,
                 bg=BG, fg=TEXT_W).pack()
        self._capture_progress = tk.Label(f, text="", font=self.fn_med,
                                           bg=BG, fg=ACCENT)
        self._capture_progress.pack(pady=10)
        return f

    def _start_capture(self):
        if self._current_screen() != "main":
            return
        self._preview_stop()
        if self.stm:
            self.stm.all_leds_off()
        self._show_screen("capturing")
        self.captured_images = []
        threading.Thread(target=self._do_capture, daemon=True).start()

    def _do_capture(self):
        if self.cam and self.cam.available:
            try:
                from picamera2 import Picamera2
                self.cam._apply_controls(preview=False)
                time.sleep(0.3)
                # Discard frames
                for _ in range(3):
                    req = self.cam.cam.capture_request()
                    req.release()

                for idx, (led, wl, duty) in enumerate(LED_TABLE):
                    self.after(0, lambda i=idx, n=len(LED_TABLE):
                               self._capture_progress.configure(
                                   text=f"Снимок {i+1} / {n}  ({wl} нм)"))
                    if self.stm:
                        self.stm.led_duty(led, duty)
                        self.stm.led_on(led)
                    time.sleep(0.005)

                    # Discard 3
                    for _ in range(3):
                        req = self.cam.cam.capture_request()
                        req.release()

                    req = self.cam.cam.capture_request()
                    buf = io.BytesIO()
                    req.save("main", buf, format="jpeg")
                    req.release()
                    self.captured_images.append((wl, buf.getvalue()))

                    if self.stm:
                        self.stm.led_off(led)

            except Exception as e:
                print(f"[CAPTURE] Error: {e}")
        else:
            # Simulate for demo
            for idx, (led, wl, duty) in enumerate(LED_TABLE):
                self.after(0, lambda i=idx, n=len(LED_TABLE):
                           self._capture_progress.configure(
                               text=f"Снимок {i+1} / {n}  ({wl} нм)"))
                # Create a colored placeholder
                color = (
                    int(450 + (wl - 450) * 0.3),
                    int(100 + (wl - 450) * 0.1),
                    int(50)
                )
                img = Image.new("RGB", (640, 480), color)
                buf = io.BytesIO()
                img.save(buf, format="jpeg")
                self.captured_images.append((wl, buf.getvalue()))
                time.sleep(0.3)

        self.after(0, self._show_photo_preview)

    # ─────────────────────────────────────────
    #  PHOTO PREVIEW SCREEN
    # ─────────────────────────────────────────
    def _build_preview_photos(self) -> tk.Frame:
        f = tk.Frame(self, bg=BG)
        f._status_bar = StatusBar(f)
        f._status_bar.pack(fill="x", side="top")

        body = tk.Frame(f, bg=BG)
        body.pack(fill="both", expand=True)

        # Photo canvas (left)
        self._photo_canvas = tk.Canvas(body, width=560, height=380,
                                        bg="#0a0a0a", highlightthickness=0)
        self._photo_canvas.pack(side="left", padx=10, pady=10)
        self._photo_img_ref = None

        # Right controls
        right = tk.Frame(body, bg=BG, width=210)
        right.pack(side="right", fill="y", padx=(5, 10), pady=10)
        right.pack_propagate(False)

        # Wavelength label
        self._wl_label = tk.Label(right, text="450 нм", font=self.fn_title,
                                   bg=BG, fg=ACCENT)
        self._wl_label.pack(pady=(10, 0))

        self._photo_idx_label = tk.Label(right, text="1 / 8",
                                          font=self.fn_small, bg=BG, fg="#888")
        self._photo_idx_label.pack()

        nav = tk.Frame(right, bg=BG)
        nav.pack(pady=10)
        RoundButton(nav, text="◀", bg=GRAY_BTN, fg=TEXT_W,
                    font=self.fn_big, width=88, height=70,
                    command=self._photo_prev).pack(side="left", padx=4)
        RoundButton(nav, text="▶", bg=GRAY_BTN, fg=TEXT_W,
                    font=self.fn_big, width=88, height=70,
                    command=self._photo_next).pack(side="left", padx=4)

        tk.Frame(right, bg=BG, height=20).pack()
        tk.Label(right, text="Сохранить?", font=self.fn_small,
                 bg=BG, fg="#888").pack()

        btn_row2 = tk.Frame(right, bg=BG)
        btn_row2.pack(pady=8)
        RoundButton(btn_row2, text="✕", bg=RED_BTN, fg=TEXT_W,
                    font=self.fn_big, width=88, height=70,
                    command=self._discard_photos).pack(side="left", padx=4)
        RoundButton(btn_row2, text="✓", bg=GREEN_BTN, fg=TEXT_W,
                    font=self.fn_big, width=88, height=70,
                    command=self._discard_photos).pack(side="left", padx=4)

        return f

    def _show_photo_preview(self):
        self.preview_idx = 0
        self._show_screen("preview_photos")
        self._render_photo()

    def _render_photo(self):
        if not self.captured_images:
            return
        idx = self.preview_idx % len(self.captured_images)
        wl, data = self.captured_images[idx]
        img = Image.open(io.BytesIO(data)).resize((560, 380), Image.LANCZOS)
        tk_img = pil_to_tk(img)
        self._photo_canvas.delete("all")
        self._photo_canvas.create_image(0, 0, anchor="nw", image=tk_img)
        self._photo_img_ref = tk_img
        self._wl_label.configure(text=f"{wl} нм")
        self._photo_idx_label.configure(
            text=f"{idx+1} / {len(self.captured_images)}")

    def _photo_prev(self):
        self.preview_idx = (self.preview_idx - 1) % max(1, len(self.captured_images))
        self._render_photo()

    def _photo_next(self):
        self.preview_idx = (self.preview_idx + 1) % max(1, len(self.captured_images))
        self._render_photo()

    def _discard_photos(self):
        self.captured_images = []
        self._show_screen("main")
        self._preview_start()
        if self.stm:
            self.stm.set_group(self.light_group.get(), PREVIEW_DUTY)

    # ─────────────────────────────────────────
    #  QUIT
    # ─────────────────────────────────────────
    def _quit(self):
        self._preview_stop()
        if self.stm:
            self.stm.close()
        if self.cam:
            self.cam.stop()
        self.destroy()


# ─────────────────────────────────────────
#  STATUS BAR WIDGET
# ─────────────────────────────────────────
class StatusBar(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=PANEL_BG, height=32, **kwargs)
        self.pack_propagate(False)

        self._stm_label = tk.Label(self, text="", font=tkfont.Font(size=11),
                                    bg=PANEL_BG, fg="#F44336")
        self._stm_label.pack(side="left", padx=8)

        self._bat_canvas = tk.Canvas(self, width=70, height=30,
                                      bg=PANEL_BG, highlightthickness=0)
        self._bat_canvas.pack(side="right", padx=8)
        self._bat_img_ref = None
        self.update_status(75, False, True)

    def update_status(self, level: int, charging: bool, stm_ok: bool):
        self._stm_label.configure(
            text="" if stm_ok else "⚠ Контроллер отключён")
        img = make_bat_icon(level, charging, size=(60, 26))
        tk_img = pil_to_tk(img)
        self._bat_canvas.delete("all")
        self._bat_canvas.create_image(5, 2, anchor="nw", image=tk_img)
        self._bat_img_ref = tk_img


# ─────────────────────────────────────────
#  ROUNDED BUTTON WIDGET
# ─────────────────────────────────────────
class RoundButton(tk.Canvas):
    def __init__(self, parent, text, bg, fg, font, width, height,
                 command=None, radius=14, **kwargs):
        super().__init__(parent, width=width, height=height,
                         bg=parent["bg"], highlightthickness=0, **kwargs)
        self._bg = bg
        self._fg = fg
        self._text = text
        self._font = font
        self._radius = radius
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self, pressed=False):
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        r = self._radius
        bg = self._darken(self._bg) if pressed else self._bg
        # Rounded rect via polygon
        pts = [
            r, 0,
            w-r, 0,
            w, r,
            w, h-r,
            w-r, h,
            r, h,
            0, h-r,
            0, r,
        ]
        self.create_polygon(pts, smooth=True, fill=bg, outline="")
        self.create_text(w//2, h//2, text=self._text,
                         fill=self._fg, font=self._font)

    def _darken(self, hex_color: str) -> str:
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            return f"#{max(r-30,0):02x}{max(g-30,0):02x}{max(b-30,0):02x}"
        except Exception:
            return hex_color

    def _on_click(self, e):
        self._draw(pressed=True)

    def _on_release(self, e):
        self._draw(pressed=False)
        if self._command:
            self._command()

    def configure(self, **kw):
        if "bg" in kw:
            self._bg = kw.pop("bg")
        super().configure(**kw)
        self._draw()


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    app = HyperspectRus()
    app.mainloop()
