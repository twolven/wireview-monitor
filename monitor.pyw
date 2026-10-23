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
        try:
            self.connection = self.events.get_nowait()
            if self.connection['state'] == 'live':
                self.sample = self.connection['sample']
                power = self.sample['power']
                self.history.append((self.sample['timestamp'], power))
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
        if event.x / self.sx > 1070 and 22 < event.y / self.sy < 65:
            self.toggle_topmost()
        elif 590 < event.x / self.sx < 800 and 22 < event.y / self.sy < 65:
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

    def metric(self, x, title, value, unit, detail, color=TEXT):
        self.rect(x, 95, 232, 113)
        self.text(x+20, 110, title, 11, MUTED, weight='bold')
        self.text(x+20, 132, value, 37, color, weight='bold')
        self.text(x+212, 150, unit, 15, MUTED, anchor='ne')
        self.text(x+20, 183, detail, 11, MUTED, width=int(192*self.sx))

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
        self.text(28, 23, 'WIREVIEW', 25, TEXT, weight='bold')
        self.text(29, 58, 'GPU CONNECTOR MONITOR', 10, MUTED, weight='bold')
        self.rect(590, 25, 210, 38, PANEL)
        self.text(695, 44, 'BACK TO MONITOR' if self.safety_view else 'LIMITS & PROTECTIONS', 10,
                  TEXT, anchor='center', weight='bold')
        self.rect(1074, 25, 178, 38, '#263326' if self.topmost else PANEL)
        self.text(1163, 44, 'PINNED ON TOP' if self.topmost else 'KEEP ON TOP', 10,
                  GREEN if self.topmost else MUTED, anchor='center', weight='bold')
        self.text(1048, 30, state, 11, GREEN if live else AMBER, anchor='ne', weight='bold')
        identity = f"{self.connection.get('port', 'USB')}   /   FW {self.connection.get('firmware', '—')}   /   2 Hz"
        self.text(1048, 51, identity if live else 'Waiting for live USB telemetry', 10, MUTED, anchor='ne')
        power = f"{s['power']:.1f}" if s else '—'
        voltage = f"{s['voltage']:.3f}" if s else '—'
        current = f"{s['current']:.2f}" if s else '—'
        temps = s['temperatures'] if s else [None]*4
        present_temps = [t for t in temps if t is not None]
        hottest = f'{max(present_temps):.1f}' if present_temps else '—'
        cfg = self.connection.get('configuration', {}) if live else {}
        power_limit = min(s['cable_watts'], cfg.get('power_limit') or s['cable_watts']) if s else None
        xs = [28 + 248 * i for i in range(5)] if PC_POWER_ENABLED else [142 + 248 * i for i in range(4)]
        if PC_POWER_ENABLED:
            pc_live = self.pc_power['state'] == 'live'
            pc_watts = f"{self.pc_power['watts']:.1f}" if pc_live else '—'
            self.metric(xs.pop(0), 'PC POWER', pc_watts, 'W', 'EcoFlow AC output' if pc_live else 'EcoFlow unavailable', TEXT if pc_live else MUTED)
        self.metric(xs[0], 'CONNECTOR POWER', power, 'W', 'Measured at the 12V-2x6 cable', self.load_color(s['power'] if s else None, power_limit, live))
        self.metric(xs[1], 'SUPPLY VOLTAGE', voltage, 'V', 'Device average voltage', ink)
        self.metric(xs[2], 'TOTAL CURRENT', current, 'A', 'Sum of all six power pins', self.load_color(s['current'] if s else None, cfg.get('current_limit'), live))
        hottest_color = self.load_color(max(present_temps) if present_temps else None, cfg.get('temperature_limit'), live)
        self.metric(xs[3], 'HOTTEST SENSOR', hottest, '°C', 'Input, output, TG / GPU 12-pin', hottest_color)
        if self.safety_view:
            self.draw_safety(live)
        else:
            self.draw_graph(live)
            self.draw_pins(live)
            self.rect(28, 505, 776, 132)
            self.text(48, 522, 'TEMPERATURES', 11, MUTED, weight='bold')
            labels = ('INPUT', 'OUTPUT', 'TG 12-PIN', 'GPU 12-PIN')
            for n, (label, value) in enumerate(zip(labels, temps)):
                x = 48+n*188
                if n:
                    self.rule(x-12, 552, x-12, 617)
                self.text(x, 553, '—' if value is None else f'{value:.1f}°', 25,
                          self.load_color(value, cfg.get('temperature_limit'), live), weight='bold')
                self.text(x, 595, label, 10, MUTED)
        if live:
            footer = f"Read-only USB monitoring    ·    Cable rating {s['cable_watts']} W    ·    Fan duty {s['fan']}%"
        elif s:
            footer = f"LAST READINGS · {time.strftime('%H:%M:%S', time.localtime(s['timestamp']))} · Reconnecting automatically"
        else:
            footer = 'Connecting to WireView Pro II · Close other apps using its USB port'
        self.text(28, 655, footer, 10, MUTED)
        self.text(1252, 655, '500 ms  /  LOCAL', 10, MUTED, anchor='ne')

    def draw_graph(self, live):
        self.rect(28, 226, 776, 263)
        self.text(48, 244, 'POWER HISTORY', 11, MUTED, weight='bold')
        stats = f'MIN {self.minimum:.1f}    AVG {self.total/self.count:.1f}    PEAK {self.peak:.1f} W' if self.count else 'Collecting samples'
        self.text(784, 244, stats, 11, MUTED, anchor='ne')
        self.text(48, 266, '5-minute view · Session min / average / peak', 9, MUTED)
        left, right, top, bottom = 48, 745, 288, 452
        now = time.time()
        entries = [(t,p) for t,p in self.history if t >= now-300]
        ceiling = max(100, math.ceil(max((p for _,p in entries), default=0)*1.15/50)*50)
        for i in range(5):
            y = top + (bottom-top)*i/4
            self.rule(left, y, right, y, '#293039')
            self.text(784, y, f'{ceiling*(1-i/4):.0f}', 10, MUTED, anchor='e')
        points = []
        previous = None
        for timestamp, power in entries:
            if previous is not None and timestamp-previous > 2.5:
                if len(points) >= 4:
                    self.canvas.create_line(*points, fill=GREEN, width=2*self.scale)
                points = []
            points.extend(((left+(timestamp-(now-300))/300*(right-left))*self.sx,
                           (bottom-power/ceiling*(bottom-top))*self.sy))
            previous = timestamp
        if len(points) >= 4:
            self.canvas.create_line(*points, fill=GREEN if live else MUTED, width=2*self.scale)
        self.text(left, 465, '5 MIN AGO', 9, MUTED)
        self.text(right, 465, 'NOW', 9, MUTED, anchor='ne')

    def draw_pins(self, live):
        self.rect(820, 226, 432, 411)
        s = self.sample
        ink = TEXT if live else MUTED
        self.text(840, 244, 'CURRENT PER PIN', 11, MUTED, weight='bold')
        self.text(1232, 244, 'A', 11, MUTED, anchor='ne')
        pins = s['pins'] if s else [None]*6
        cfg = self.connection.get('configuration', {}) if live else {}
        reference = pin_reference(cfg)
        scale = max(10, math.ceil(reference*1.1), math.ceil(max((p['current'] for p in pins if p), default=0)))
        for n, pin in enumerate(pins):
            y = 284+n*35
            self.text(840, y, f'0{n+1}', 11, MUTED)
            self.rect(878, y+4, 265, 10, '#2a323b')
            if pin:
                color = self.load_color(pin['current'], reference, live)
                self.rect(878, y+4, min(265, 265*pin['current']/scale), 10, color)
            marker = 878+265*reference/scale
            self.rule(marker, y+1, marker, y+17, RED if live else MUTED)
            self.text(1232, y-3, f"{pin['current']:.3f}" if pin else '—', 17,
                      self.load_color(pin['current'] if pin else None, reference, live), anchor='ne', weight='bold')
        spread = max(p['current'] for p in pins)-min(p['current'] for p in pins) if s else None
        self.text(840, 503, 'PIN SPREAD', 10, MUTED)
        self.text(1232, 501, f'{spread:.3f} A' if spread is not None else '—', 12, ink, anchor='ne')
        self.text(840, 524, f'Amber ≥{reference*.9:.2f} A · Red ≥{reference:g} A', 10, MUTED)
        self.rule(840, 549, 1232, 549)
        active = s['active_faults'] if s else None
        logged = s['logged_faults'] if s else None
        if not live:
            headline, color = 'NO LIVE FAULT STATUS', AMBER
        elif active:
            headline, color = 'DEVICE FAULT ACTIVE', RED
        else:
            headline, color = 'NO ACTIVE DEVICE FAULTS', GREEN
        self.text(840, 565, headline, 11, color, weight='bold')
        detail = ', '.join(fault_names(active)) if live and active else ('No logged faults' if logged == 0 else 'Logged: '+', '.join(fault_names(logged or 0)))
        if not live:
            detail = str(self.connection.get('error', 'Waiting for device'))
        self.text(840, 589, detail, 10, MUTED, width=int(390*self.sx))

    def draw_safety(self, live):
        self.rect(28, 226, 1224, 411)
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
