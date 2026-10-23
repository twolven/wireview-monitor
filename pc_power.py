"""Optional PC-power card: reads a local health.json written by an EcoFlow bridge.

Enabled only when WIREVIEW_PC_POWER_FILE points at that file; no BLE or network access here.
"""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import time

ENABLED = bool(os.environ.get('WIREVIEW_PC_POWER_FILE'))
ENABLED = bool(os.environ.get('WIREVIEW_PC_POWER_FILE'))
HEALTH_FILE = Path(os.environ.get('WIREVIEW_PC_POWER_FILE', ''))
MAX_AGE = 5.0


def read_pc_power(path=HEALTH_FILE, now=None):
    unavailable = dict(state='unavailable', watts=None)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        timestamp = datetime.fromisoformat(data['time']).timestamp()
        now = time.time() if now is None else now
        age = now - timestamp
        field_age = data['field_age_seconds']['ac_output_power']
        watts = data['values']['ac_output_power']
        if (data.get('connected') is not True or data.get('fresh') is not True
                or not -2 <= age <= MAX_AGE
                or isinstance(field_age, bool) or not isinstance(field_age, (int, float))
                or not math.isfinite(field_age) or not 0 <= field_age <= MAX_AGE
                or isinstance(watts, bool) or not isinstance(watts, (int, float))
                or not math.isfinite(watts) or watts < 0):
            return unavailable
        return dict(state='live', watts=float(watts), timestamp=timestamp)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return unavailable
