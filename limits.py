"""Display guidance only. Firmware owns its alarm timing and protection actions."""
import math

PIN_REFERENCE_AMPS = 9.2  # Molex Micro-Fit+ PCIe 12V-2x6 published contact rating.


def pin_reference(configuration):
    setting = (configuration or {}).get('wire_current_limit', 0)
    return min(PIN_REFERENCE_AMPS, setting) if setting > 0 else PIN_REFERENCE_AMPS


def load_level(value, limit):
    if value is None or limit is None or not math.isfinite(limit) or limit <= 0:
        return 'unknown'
    if value >= limit:
        return 'high'
    if value >= limit * 0.9:
        return 'near'
    return 'normal'
