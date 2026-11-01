"""Compact, read-only native monitor for the Thermal Grizzly WireView Pro II."""
import collections
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk

from protocol import Device, fault_names, FAULTS
from limits import pin_reference, load_level
from pc_power import ENABLED as PC_POWER_ENABLED, read_pc_power
from sysmon import Sensors

BASE = Path(__file__).resolve().parent
BG, PANEL, LINE = '#111418', '#1b2026', '#303842'
TEXT, MUTED, GREEN, AMBER, RED = '#f0f3f5', '#929da9', '#b3ed8b', '#ffce79', '#ff8585'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[RotatingFileHandler(BASE / 'monitor.log', maxBytes=1_000_000, backupCount=2)])


def publish_status(value):
    value = dict(value, pc_power=read_pc_power())
    pending = BASE / 'status.tmp'
    pending.write_text(json.dumps(value, indent=2), encoding='utf-8')
    pending.replace(BASE / 'status.json')


# (MSI Afterburner source, label, unit, decimals, trace range: (lo, hi) | 'max' | None=auto)
TEMP_COLORS = (GREEN, '#7cc4ff', AMBER, '#d9a0ff')

TILES = (
    ('CPU usage', 'CPU USAGE', '%', 0, (0, 100)),
    ('GPU2 usage', 'GPU USAGE', '%', 0, (0, 100)),
    ('RAM usage', 'RAM USED', 'MB', 0, 'max'),
    ('GPU2 memory usage', 'GPU VRAM', 'MB', 0, 'max'),
    ('CPU clock', 'CPU CLOCK', 'MHz', 0, None),
    ('GPU2 core clock', 'GPU CORE CLOCK', 'MHz', 0, None),
    ('GPU2 memory clock', 'GPU MEM CLOCK', 'MHz', 0, None),
    ('CPU temperature', 'CPU TEMP', '°C', 0, (0, 100)),
    ('GPU2 temperature', 'GPU TEMP', '°C', 0, (0, 100)),
    ('CPU power', 'CPU POWER', 'W', 1, None),
    ('GPU2 voltage', 'GPU VOLTAGE', 'V', 3, None),
)
GROUPS = (('LOAD & MEMORY', 4), ('CLOCKS', 3), ('TEMPERATURE & POWER', 4))
SAMPLED = tuple(name for name, *_ in TILES) + ('GPU2 power',)


def worker(events, stop):
    def post(value):
        try:
            events.get_nowait()
        except queue.Empty:
            pass
        events.put_nowait(value)

    device = None
    count = 0
    configuration = None
    next_configuration = 0
    try:
        while not stop.is_set():
            try:
                if device is None:
                    device = Device()
                    logging.info('Connected %s firmware=%s uid=%s', device.port, device.firmware, device.uid)
                    next_configuration = 0
                if time.monotonic() >= next_configuration:
                    configuration = device.configuration()
                    next_configuration = time.monotonic() + 30
                    logging.info('Device protection settings: %s', configuration)
                sample = device.sample()
                count += 1
                event = dict(state='live', sample=sample, port=device.port,
                             firmware=device.firmware, configuration=configuration,
                             samples=count, pid=os.getpid())
                post(event)
                try:
                    publish_status(event)
                except OSError:
                    logging.exception('Cannot write status; USB monitoring continues')
                stop.wait(0.5)
            except Exception as error:
                logging.warning('Telemetry unavailable: %s', error)
                if device is not None:
                    device.close()
                    device = None
                event = dict(state='disconnected', error=str(error), timestamp=time.time(), samples=count)
                post(event)
                try:
                    publish_status(event)
                except OSError:
                    logging.exception('Cannot write status')
                stop.wait(3)
    finally:
        if device is not None:
            device.close()
        try:
            publish_status(dict(state='stopped', timestamp=time.time(), samples=count))
        except OSError:
            logging.exception('Cannot write final status')


class Monitor:
    def __init__(self, root):
        self.root = root
        root.title('WireView Monitor')
        root.configure(bg=BG)
        root.geometry('1248x664')
        root.minsize(850, 480)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _: self.draw())
        self.canvas.bind('<Button-1>', self.clicked)
        root.bind('t', lambda _: self.toggle_topmost())
        root.bind('s', lambda _: self.toggle_safety())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = lambda kind, value, trace: logging.error(
            'UI callback failed', exc_info=(kind, value, trace))
        self.topmost = False
        self.safety_view = False
        self.events = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.sample = None
        self.pc_power = read_pc_power()
        self.sensors = Sensors()
        self.system = None
        self.system_history = {name: collections.deque(maxlen=300) for name in SAMPLED}
        self.next_system_sample = 0.0
        self.temp_history = [collections.deque(maxlen=300) for _ in range(4)]
        self.next_temp_sample = 0.0
        self.connection = dict(state='connecting')
        self.history = collections.deque(maxlen=1200)
        self.total = 0.0
        self.count = 0
        self.peak = 0.0
        self.minimum = None
        self.started = time.time()
        self.thread = threading.Thread(target=worker, args=(self.events, self.stop), daemon=True)
        self.thread.start()
        root.after(100, self.style_titlebar)
        root.after(150, self.fit_quarter)
        root.after(200, self.tick)

    def style_titlebar(self):
        """Style only this application's own Windows frame; no desktop inspection."""
        if os.name != 'nt':
            return
        try:
            from ctypes import wintypes
            # Tk supplies its own outer frame handle, without searching other windows.
            frame = int(str(self.root.tk.call('wm', 'frame', self.root._w)), 0)
            set_attribute = ctypes.WinDLL('dwmapi').DwmSetWindowAttribute
            set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            set_attribute.restype = ctypes.c_long

            def colorref(color):
                red, green, blue = (int(color[i:i+2], 16) for i in (1, 3, 5))
                return red | (green << 8) | (blue << 16)

            # Dark caption buttons, matching border/caption, and light caption text.
            attributes = ((20, 1), (34, colorref(BG)), (35, colorref(BG)), (36, colorref(TEXT)))
            results = {}
            for attribute, value in attributes:
                data = ctypes.c_uint32(value)
                results[attribute] = set_attribute(frame, attribute, ctypes.byref(data), ctypes.sizeof(data))
            if all(result == 0 for result in results.values()):
                logging.info('Dark title bar applied: background=%s text=%s', BG, TEXT)
            else:
                logging.warning('Title bar styling results (HRESULT): %s', results)
        except Exception:
            logging.exception('Unable to style title bar')

    def fit_quarter(self):
        # Measure only this app's own decorations to keep its outer size <=1280x720.
        self.root.update_idletasks()
        frame_x = max(0, self.root.winfo_rootx() - self.root.winfo_x())
        frame_y = max(0, self.root.winfo_rooty() - self.root.winfo_y())
        self.root.geometry(f'{1280 - 2 * frame_x}x{720 - frame_y - frame_x}')
        logging.info('Window client=%sx%s; target outer=1280x720',
                     1280 - 2 * frame_x, 720 - frame_y - frame_x)

    def tick(self):
        self.pc_power = read_pc_power()
        self.system = self.sensors.read()
        now = time.time()
        if self.system and now >= self.next_system_sample:
            self.next_system_sample = now + 1.0
            for name in SAMPLED:
                if name in self.system:
                    self.system_history[name].append((now, self.system[name]['value']))
        try:
            self.connection = self.events.get_nowait()
            if self.connection['state'] == 'live':
                self.sample = self.connection['sample']
                power = self.sample['power']
                self.history.append((self.sample['timestamp'], power))
                if now >= self.next_temp_sample:
                    self.next_temp_sample = now + 1.0
                    for trace, value in zip(self.temp_history, self.sample['temperatures']):
                        if value is not None:
                            trace.append((now, value))
                self.total += power
                self.count += 1
                self.peak = max(self.peak, power)
                self.minimum = power if self.minimum is None else min(self.minimum, power)
        except queue.Empty:
            pass
        self.draw()
        self.root.after(250, self.tick)

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes('-topmost', self.topmost)
        self.draw()

    def clicked(self, event):
        if event.x / self.sx > 1070 and 10 < event.y / self.sy < 52:
            self.toggle_topmost()
        elif 590 < event.x / self.sx < 800 and 10 < event.y / self.sy < 52:
            self.toggle_safety()

    def toggle_safety(self):
        self.safety_view = not self.safety_view
        self.draw()

    def load_color(self, value, limit, live):
        if not live:
            return MUTED
        return {'normal': GREEN, 'near': AMBER, 'high': RED, 'unknown': MUTED}[load_level(value, limit)]

    def text(self, x, y, value, size=13, fill=TEXT, anchor='nw', weight='normal', **kw):
        return self.canvas.create_text(x * self.sx, y * self.sy, text=value, fill=fill,
                                       anchor=anchor, font=('Segoe UI', -max(9, int(size * self.scale)), weight), **kw)

    def rect(self, x, y, w, h, fill=PANEL, outline=''):
        self.canvas.create_rectangle(x*self.sx, y*self.sy, (x+w)*self.sx, (y+h)*self.sy,
                                     fill=fill, outline=outline, width=1)

    def rule(self, x1, y1, x2, y2, fill=LINE, width=1):
        self.canvas.create_line(x1*self.sx, y1*self.sy, x2*self.sx, y2*self.sy,
                                fill=fill, width=max(1, width*self.scale))

    def metric(self, x, title, value, unit, detail, color=TEXT, trace=None):
        w = 294
        self.rect(x, 62, w, 82)
        self.text(x+16, 71, title, 10, MUTED, weight='bold')
        self.text(x+16, 86, value, 30, color, weight='bold')
        if trace:
            self.text(x+112, 102, unit, 13, MUTED)
        else:
            self.text(x+w-16, 102, unit, 13, MUTED, anchor='ne')
        self.text(x+16, 124, detail, 9, MUTED, width=int((130 if trace else 200)*self.sx))
        if trace:
            self.trace(trace, x+150, x+w-14, 76, 132, color=GREEN)

    def trace(self, entries, left, right, top, bottom, low=0, high=None, color=GREEN, width=1.3):
        """Five-minute line from (timestamp, value) pairs; baseline drawn at the bottom."""
        now = time.time()
        entries = [(t, v) for t, v in entries if t >= now-300]
        self.rule(left, bottom, right, bottom, '#293039')
        if len(entries) < 2:
            return
        peak = max(v for _, v in entries)
        high = max(high if high is not None else peak*1.2, peak, low + 1e-6)
        points = []
        for t, v in entries:
            points.extend(((left+(t-(now-300))/300*(right-left))*self.sx,
                           (bottom-(v-low)/(high-low)*(bottom-top))*self.sy))
        self.canvas.create_line(*points, fill=color, width=max(1, width*self.scale))

    def draw(self):
        if not hasattr(self, 'connection'):
            return
        self.sx = self.canvas.winfo_width() / 1280
        self.sy = self.canvas.winfo_height() / 680
        self.scale = min(self.sx, self.sy)
        self.canvas.delete('all')
        s = self.sample
        live = bool(s and self.connection['state'] == 'live' and time.time()-s['timestamp'] < 2.5)
        ink = TEXT if live else MUTED
        state = 'LIVE' if live else ('CONNECTING' if s is None and self.connection['state'] == 'connecting' else 'DISCONNECTED')
        self.text(28, 10, 'WIREVIEW', 22, TEXT, weight='bold')
        self.text(30, 40, 'GPU CONNECTOR + SYSTEM MONITOR', 9, MUTED, weight='bold')
        self.rect(590, 10, 210, 38, PANEL)
        self.text(695, 29, 'BACK TO MONITOR' if self.safety_view else 'LIMITS & PROTECTIONS', 10,
                  TEXT, anchor='center', weight='bold')
        self.rect(1074, 10, 178, 38, '#263326' if self.topmost else PANEL)
        self.text(1163, 29, 'PINNED ON TOP' if self.topmost else 'KEEP ON TOP', 10,
                  GREEN if self.topmost else MUTED, anchor='center', weight='bold')
        self.text(1048, 14, state, 11, GREEN if live else AMBER, anchor='ne', weight='bold')
        identity = f"{self.connection.get('port', 'USB')}   /   FW {self.connection.get('firmware', '—')}   /   2 Hz"
        self.text(1048, 33, identity if live else 'Waiting for live USB telemetry', 10, MUTED, anchor='ne')
        power = f"{s['power']:.1f}" if s else '—'
        current = f"{s['current']:.2f}" if s else '—'
        temps = s['temperatures'] if s else [None]*4
        cfg = self.connection.get('configuration', {}) if live else {}
        power_limit = min(s['cable_watts'], cfg.get('power_limit') or s['cable_watts']) if s else None
        pc_live = self.pc_power['state'] == 'live'
        pc_watts = f"{self.pc_power['watts']:.1f}" if pc_live else '—'
        gpu_power = self.system.get('GPU2 power') if self.system else None
        gpu_watts = f"{gpu_power['value']:.1f}" if gpu_power else '—'
        # Cards are 294 wide; with the optional PC power card off, the other three spread across the row.
        xs = [28, 338, 648, 958] if PC_POWER_ENABLED else [28, 493, 958]
        if PC_POWER_ENABLED:
            self.metric(xs.pop(0), 'PC POWER', pc_watts, 'W', 'EcoFlow AC output' if pc_live else 'EcoFlow unavailable', TEXT if pc_live else MUTED)
        self.metric(xs[0], 'GPU BOARD POWER', gpu_watts, 'W', 'Whole card · 5 min' if gpu_power else 'Afterburner not running',
                    TEXT if gpu_power else MUTED, trace=self.system_history['GPU2 power'])
        stats = f'MIN {self.minimum:.0f}  AVG {self.total/self.count:.0f}  PEAK {self.peak:.0f}' if self.count else '12V-2x6 cable'
        self.metric(xs[1], 'CONNECTOR POWER', power, 'W', stats, self.load_color(s['power'] if s else None, power_limit, live),
                    trace=self.history)
        self.metric(xs[2], 'TOTAL CURRENT', current, 'A', 'Sum of all six power pins', self.load_color(s['current'] if s else None, cfg.get('current_limit'), live))
        if live:
            note = ('Read-only USB monitoring  ·  500 ms / LOCAL',
                    f"Cable rating {s['cable_watts']} W  ·  Fan duty {s['fan']}%")
        elif s:
            note = (f"LAST READINGS · {time.strftime('%H:%M:%S', time.localtime(s['timestamp']))}",
                    'Reconnecting automatically')
        else:
            note = ('Connecting to WireView Pro II', 'Close other apps using its USB port')
        self.text(250, 14, note[0], 10, MUTED)
        self.text(250, 33, note[1], 10, MUTED)
        if self.safety_view:
            self.draw_safety(live)
        else:
            self.draw_pins(live)
            self.draw_system()
            self.draw_temps(live, temps, cfg)

    def draw_temps(self, live, temps, cfg):
        self.rect(820, 400, 432, 264)
        self.text(840, 412, 'WIREVIEW TEMPERATURES', 11, MUTED, weight='bold')
        self.text(1232, 412, '5-minute trace', 9, MUTED, anchor='ne')
        labels = ('INPUT', 'OUTPUT', 'TG 12-PIN', 'GPU 12-PIN')
        for n, (label, value) in enumerate(zip(labels, temps)):
            x = 840 + n * 98
            self.text(x, 432, '—' if value is None else f'{value:.1f}°', 19,
                      self.load_color(value, cfg.get('temperature_limit'), live), weight='bold')
            self.rect(x, 461, 8, 3, TEMP_COLORS[n])
            self.text(x + 13, 456, label, 9, MUTED)
        now = time.time()
        entries = [[(t, v) for t, v in trace if t >= now-300] for trace in self.temp_history]
        values = [v for trace in entries for _, v in trace]
        left, right, top, bottom = 868, 1232, 484, 550
        if values:
            low, high = min(values), max(values)
            if high - low < 8:
                mid = (high + low) / 2
                low, high = mid - 4, mid + 4
            low, high = math.floor(low - 1), math.ceil(high + 1)
        else:
            low, high = 20, 40
        for i in range(3):
            y = top + (bottom - top) * i / 2
            self.rule(left, y, right, y, '#293039')
            self.text(left - 6, y, f'{high - (high - low) * i / 2:.0f}°', 9, MUTED, anchor='e')
        for n, trace in enumerate(entries):
            if len(trace) < 2:
                continue
            points = []
            for t, v in trace:
                points.extend(((left + (t - (now - 300)) / 300 * (right - left)) * self.sx,
                               (bottom - (v - low) / (high - low) * (bottom - top)) * self.sy))
            self.canvas.create_line(*points, fill=TEMP_COLORS[n] if live else MUTED,
                                    width=max(1, 1.5 * self.scale))
        self.rule(840, 568, 1232, 568)
        s = self.sample
        active = s['active_faults'] if s else None
        logged = s['logged_faults'] if s else None
        if not live:
            headline, color = 'NO LIVE FAULT STATUS', AMBER
        elif active:
            headline, color = 'DEVICE FAULT ACTIVE', RED
        else:
            headline, color = 'NO ACTIVE DEVICE FAULTS', GREEN
        self.text(840, 582, headline, 11, color, weight='bold')
        detail = ', '.join(fault_names(active)) if live and active else ('No logged faults' if logged == 0 else 'Logged: '+', '.join(fault_names(logged or 0)))
        if not live:
            detail = str(self.connection.get('error', 'Waiting for device'))
        self.text(840, 604, detail, 10, MUTED, width=int(390*self.sx))

    def draw_system(self):
        self.rect(28, 152, 776, 512)
        self.text(48, 164, 'CPU · GPU · MEMORY', 11, MUTED, weight='bold')
        source = 'MSI Afterburner · 1 Hz · 5-minute traces' if self.system else 'MSI Afterburner not running — start it (tray only) for CPU/GPU data'
        self.text(784, 164, source, 10, AMBER if not self.system else MUTED, anchor='ne')
        height = 137
        n = 0
        for row, (caption, count) in enumerate(GROUPS):
            width = (752 - (count - 1) * 8) / count
            block = 184 + row * 161
            self.text(42, block, caption, 9, MUTED, weight='bold')
            y = block + 16
            for col in range(count):
                name, label, unit, places, span = TILES[n]
                n += 1
                x = 40 + col * (width + 8)
                reading = self.system.get(name) if self.system else None
                self.rect(x, y, width, height, '#171c22')
                self.text(x+12, y+10, label, 10, MUTED, weight='bold')
                value = f"{reading['value']:.{places}f}" if reading else '—'
                self.text(x+12, y+28, value, 24, TEXT if reading else MUTED, weight='bold')
                self.text(x+width-12, y+40, unit, 10, MUTED, anchor='ne')
                if span == 'max':
                    low, high = 0, (reading['max'] if reading and reading['max'] > 0 else None)
                elif span:
                    low, high = span
                else:
                    low, high = 0, None
                self.trace(self.system_history[name], x+12, x+width-12, y+72, y+height-14,
                           low, high, GREEN if reading else MUTED, 1.6)

    def draw_pins(self, live):
        self.rect(820, 152, 432, 240)
        s = self.sample
        ink = TEXT if live else MUTED
        self.text(840, 162, 'CURRENT PER PIN', 11, MUTED, weight='bold')
        self.text(1232, 162, 'A', 11, MUTED, anchor='ne')
        pins = s['pins'] if s else [None]*6
        cfg = self.connection.get('configuration', {}) if live else {}
        reference = pin_reference(cfg)
        scale = max(10, math.ceil(reference*1.1), math.ceil(max((p['current'] for p in pins if p), default=0)))
        for n, pin in enumerate(pins):
            y = 188+n*29
            self.text(840, y, f'0{n+1}', 11, MUTED)
            self.rect(878, y+4, 265, 10, '#2a323b')
            if pin:
                color = self.load_color(pin['current'], reference, live)
                self.rect(878, y+4, min(265, 265*pin['current']/scale), 10, color)
            marker = 878+265*reference/scale
            self.rule(marker, y+1, marker, y+17, RED if live else MUTED)
            self.text(1232, y-2, f"{pin['current']:.3f}" if pin else '—', 15,
                      self.load_color(pin['current'] if pin else None, reference, live), anchor='ne', weight='bold')
        spread = max(p['current'] for p in pins)-min(p['current'] for p in pins) if s else None
        self.rule(840, 368, 1232, 368)
        self.text(840, 373, f'SPREAD  {spread:.3f} A' if spread is not None else 'SPREAD  —', 10, ink)
        self.text(1232, 373, f'Amber ≥{reference*.9:.2f} A · Red ≥{reference:g} A', 9, MUTED, anchor='ne')

    def draw_safety(self, live):
        before = set(self.canvas.find_all())
        self.draw_safety_content(live)
        for item in set(self.canvas.find_all()) - before:
            self.canvas.move(item, 0, -74 * self.sy)

    def draw_safety_content(self, live):
        self.rect(28, 226, 1224, 512)
        cfg = self.connection.get('configuration', {}) if live else {}
        self.text(48, 245, 'DEVICE ALARM SETTINGS', 12, TEXT, weight='bold')
        self.text(48, 269, 'Read from WireView · Refreshed every 30 seconds', 10, MUTED)
        fields = (('Temperature', 'temperature_limit', '°C'),
                  ('Total current', 'current_limit', 'A'),
                  ('Per-pin current', 'wire_current_limit', 'A'),
                  ('Total power', 'power_limit', 'W'),
                  ('Imbalance', 'imbalance_percent', '%'),
                  ('Imbalance minimum current', 'imbalance_min_current', 'A'),
                  ('Shutdown wait', 'shutdown_wait', 's'))
        for n, (label, key, unit) in enumerate(fields):
            y = 305+n*31
            self.text(48, y, label, 12, MUTED)
            value = f'{cfg[key]:g} {unit}' if key in cfg else 'Unavailable'
            self.text(545, y, value, 14, TEXT if live else MUTED, anchor='ne')
        self.rule(590, 247, 590, 608)
        self.text(620, 245, 'ONBOARD PROTECTION ACTIONS', 12, TEXT, weight='bold')
        self.text(620, 269, 'Configured actions; shutdown wiring is not verified', 10, MUTED)
        columns = (('Display', 'display_mask', 904), ('Buzzer', 'buzzer_mask', 998),
                   ('Soft off', 'soft_shutdown_mask', 1092), ('Hard off', 'hard_shutdown_mask', 1186))
        for title, _, x in columns:
            self.text(x, 304, title, 10, MUTED, anchor='n', weight='bold')
        for n, fault in enumerate(FAULTS):
            y = 333+n*28
            active = bool(live and self.sample['active_faults'] & (1 << n))
            self.text(620, y, fault, 11, RED if active else TEXT)
            for _, key, x in columns:
                enabled = cfg.get(key, 0) & (1 << n)
                self.text(x, y, ('ON' if enabled else 'off') if live else '—', 11,
                          GREEN if live and enabled else MUTED, anchor='n')
        reference = pin_reference(cfg)
        self.text(48, 542, f'BAR REFERENCE  {reference:g} A / pin', 11, TEXT, weight='bold')
        self.text(48, 566, 'Lower of 9.2 A contact reference and device pin alarm.\nAmber at 90%; red at reference. Exact cable/contact ratings vary.',
                  10, MUTED, width=int(510*self.sx))
        self.text(620, 522, 'Hardware owns alarm timing and shutdown actions.', 11, TEXT, weight='bold')
        self.text(620, 549, 'This app displays readings and faults; it does not issue shutdowns.\nUSB alone does not provide the motherboard shutdown connection.\nNo protection settings have been changed.', 10, MUTED, width=int(590*self.sx))

    def close(self):
        logging.info('Window closed')
        self.stop.set()
        self.sensors.close()
        self.thread.join(timeout=2.5)
        self.root.destroy()


if __name__ == '__main__':
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    # A named mutex prevents two copies competing for the same serial port.
    ctypes.windll.kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\WireViewCompactMonitor')
    if not handle:
        raise ctypes.WinError()
    if ctypes.windll.kernel32.GetLastError() != 183:
        try:
            logging.info('Starting WireView Monitor')
            root = tk.Tk()
            app = Monitor(root)
            root.mainloop()
        except Exception:
            logging.exception('Monitor failed')
            raise
