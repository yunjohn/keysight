from __future__ import annotations


def display_channel_name(channel: str) -> str:
    if channel.startswith("CHANnel"):
        return channel.replace("CHANnel", "CH", 1)
    return channel


def normalize_channel_name(channel: str) -> str:
    normalized = channel.strip()
    if normalized.upper().startswith("CH") and normalized[2:].isdigit():
        return f"CHANnel{normalized[2:]}"
    return normalized


def format_peak_current(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.value:.6f} A"


def format_peak_time(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.time_s:.6e} s"


def format_range_ms(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.3f} ~ {max(values):.3f} ms"


def format_range_amp(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} A"


def format_range_hz(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} Hz"
