from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import Qt


class InteractionTool(StrEnum):
    ZOOM = "ZOOM"
    PAN = "PAN"
    CURSOR_A = "CURSOR_A"
    CURSOR_B = "CURSOR_B"
    ANNOTATE = "ANNOTATE"


@dataclass(frozen=True)
class ChannelStyle:
    color: str
    pen_style: Qt.PenStyle
    width: float = 2.0


@dataclass(frozen=True)
class WaveformTheme:
    name: str
    window_background: str
    panel_background: str
    chart_background: str
    plot_background: str
    grid_major: str
    grid_minor: str
    axis_text: str
    primary_text: str
    secondary_text: str
    border: str
    overlay_background: str
    cursor_a: str
    cursor_b: str
    crosshair: str
    annotation: str
    preview: str
    success: str
    warning: str
    error: str
    reference_alpha: int
    channels: dict[str, ChannelStyle]

    def channel_style(self, channel: str) -> ChannelStyle:
        return self.channels.get(channel, ChannelStyle("#e5e7eb", Qt.SolidLine))


INSTRUMENT_DARK_THEME = WaveformTheme(
    name="instrument-dark",
    window_background="#11151b",
    panel_background="#1a2029",
    chart_background="#12171e",
    plot_background="#090d12",
    grid_major="#33404f",
    grid_minor="#222b36",
    axis_text="#d7dee8",
    primary_text="#eef3f8",
    secondary_text="#9aa8b8",
    border="#3a4654",
    overlay_background="rgba(20, 26, 34, 220)",
    cursor_a="#ff8a65",
    cursor_b="#b388ff",
    crosshair="#90a4ae",
    annotation="#80cbc4",
    preview="#ffd166",
    success="#5fbf8f",
    warning="#e5ad48",
    error="#ef6b73",
    reference_alpha=125,
    channels={
        "CHANnel1": ChannelStyle("#ffd84d", Qt.SolidLine),
        "CHANnel2": ChannelStyle("#56d364", Qt.DashLine),
        "CHANnel3": ChannelStyle("#58a6ff", Qt.DotLine),
        "CHANnel4": ChannelStyle("#f778ba", Qt.DashDotLine),
    },
)


def relative_luminance(color: str) -> float:
    value = color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"颜色必须为 #RRGGBB: {color}")
    channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [item / 12.92 if item <= 0.04045 else ((item + 0.055) / 1.055) ** 2.4 for item in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)
