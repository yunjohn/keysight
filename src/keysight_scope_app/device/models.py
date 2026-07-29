from __future__ import annotations

from dataclasses import dataclass, field

from keysight_scope_app.analysis.waveform import WaveformData


@dataclass(frozen=True)
class InstrumentCapabilities:
    identity: str
    waveform_points_modes: tuple[str, ...] = ("NORMal",)
    max_waveform_points: int | None = None
    screenshot_formats: tuple[str, ...] = ("PNG",)
    supports_edge_trigger: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureRequest:
    channels: tuple[str, ...]
    points_mode: str = "NORMal"
    points: int = 1000


@dataclass(frozen=True)
class CaptureResult:
    request: CaptureRequest
    waveforms: tuple[WaveformData, ...]
    channel_units: dict[str, str] = field(default_factory=dict)
    vertical_layouts: dict[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    elapsed_s: float = 0.0
