# WireView Monitor

A compact native Windows monitor for the Thermal Grizzly WireView Pro II.
Window size, including decorations, is targeted at **1280 × 720 pixels**: one
quarter of a 2560 × 1440 display. Drag it into the desired quarter or use Windows
Snap. It also resizes down to 850 × 480 client pixels.
The Windows title bar and border use the dashboard's dark background with light
caption text, preserving standard dragging, resizing, minimize, and close controls.

Windows only (it uses the Win32 title-bar and DPI APIs). Create the isolated Python environment
once with [uv](https://docs.astral.sh/uv/), then start it with `Launch.ps1`. The only dependency is
pyserial 3.5.

```powershell
git clone https://github.com/twolven/wireview-monitor
cd wireview-monitor
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
./Launch.ps1
```

## Readings

- Optional PC Power card, left of Connector Power: AC output watts of an EcoFlow UPS, read from the
  `health.json` of the [BLE bridge](https://github.com/twolven/ecoflow-delta3-windows-ups/tree/main/ble).
  Off by default (the other four cards then spread across the row); enable with
  `setx WIREVIEW_PC_POWER_FILE "C:\ProgramData\EcoFlowBLE\health.json"` and restart the monitor. The value
  covers every load on the EcoFlow's AC outputs, not just this PC. A disconnected bridge or a reading older
  than five seconds shows a dash. It opens no connection of its own.
- Connector power, supply voltage, total current, and hottest of all four sensors.
- Five-minute power history with session minimum, arithmetic mean, and peak.
- All six per-pin currents on the same scale, plus highest-to-lowest spread.
- Input/output and both external temperatures. External 1 is labeled TG 12-pin;
  external 2 is GPU 12-pin, matching the common placement; edit `labels` in `monitor.pyw` if yours differs. Absent sensors display a dash.
- Device-reported active and logged fault flags, cable rating, and fan duty.
- USB sampling every 500 ms. Disconnections grey the last readings, remove the
  live status, and retry every three seconds. Graphs leave a gap across outages.

Click **KEEP ON TOP** or press **T** to toggle the floating window.

Click **LIMITS & PROTECTIONS** or press **S** to see the actual device alarm
thresholds and per-fault display, buzzer, soft-shutdown, and hard-shutdown settings.
The complete active configuration is read every 30 seconds; it is never changed.
The physical motherboard shutdown connection cannot be verified over USB.

Pin bars color by load: green below 90% of the display reference, amber from
90%, and red at/above that reference. The reference is the **lower of 9.2 A and
the configured device per-pin alarm**. A red marker shows it on each bar. The
9.2 A value comes from Molex's Micro-Fit+ PCIe 12V-2x6 data sheet; it is a display
reference, not confirmation of this PC's particular contact/cable rating.
Other connector products have different ratings (for example, Amphenol lists
9.5 A). Load colors are immediate guidance; actual device fault bits remain
authoritative for its own configured alarm timing and actions. Green is not
a guarantee of connection safety.

The total power card colors against the lower of cable rating and device power
alarm, total current against the device total-current alarm, and every temperature
against the device temperature alarm. Unknown/stale readings are grey.

The power is measured on the GPU power cable; it excludes PCIe slot power. The
displayed connector temperatures are not GPU die temperatures. Per-pin spread is
an absolute current difference, not a replacement for the device's fault logic.

## USB access and privacy

This app talks directly to the WireView USB serial port (automatically discovered
by VID 0483/PID 5740) at 115200 baud. Only commands 01 (identity), 02 (UID),
04 (live sensors), and 05 (read active configuration) are permitted by its transport. It does not change firmware,
configuration, thresholds, GPU settings, or device fault registers.

Close the stock WireView app and any other program using this port before
launching. Only one program can own the serial port. Two copies of this monitor
are prevented by a named mutex.

## Startup

`Install-Startup.ps1` registers the monitor under the HKCU Run key `WireViewMonitor` (`-Remove` undoes it). It
does not touch any other program's startup entries. If you also run the stock WireView app, disable its
autostart yourself: only one program can own the serial port. (`Install-Startup.ps1` was written for this
public release and has been syntax-checked, not run through a sign-in.)

Keep the stock app for firmware and configuration work. Close this monitor first, then open the stock app.
Onboard alarms and protection actions operate independently of either desktop application.

The application has no network code, cloud integration, desktop capture, or app
inspection. The live source is the WireView device, plus the optional PC-power file described above.
Logs stay here in
`monitor.log` (rotated at 1 MB) and `status.json` (latest sample only). These files
include the device identity and readings; status updates every 500 ms.

Protocol reference (community documentation; this project is not affiliated with Thermal Grizzly):
https://github.com/Gustav0ar/wireview-pro-ii/blob/main/docs/protocol.md

Contact rating references:
https://www.molex.com/content/dam/molex/molex-dot-com/en_us/pdf/datasheets/987651-9552.pdf
https://cdn.amphenol-cs.com/media/wysiwyg/files/documentation/gs-12-1706.pdf

## Files

- `monitor.pyw`: native Tk canvas dashboard and reconnecting telemetry worker.
- `protocol.py`: narrowly restricted USB reader and packet decoding.
- `limits.py`: display-only load levels; no hardware or shutdown control.
- `pc_power.py`: optional PC-power card; validates freshness of the bridge file.
- `Install-Startup.ps1`: register/remove start at sign-in.
- `test_protocol.py`: sensor and reconnect failure checks.
- `Launch.ps1`: launch with the isolated environment, no console.

To run checks:

```powershell
& ./.venv/Scripts/python.exe -m unittest -v test_protocol
```

## License

MIT. See `LICENSE`.
