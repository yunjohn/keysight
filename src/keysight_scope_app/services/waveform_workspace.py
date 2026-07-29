from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from keysight_scope_app.analysis.waveform import WaveformData
from keysight_scope_app.services.quality import DataQualityIssue, inspect_waveforms

WORKSPACE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WaveformFrame:
    sequence: int
    captured_at: str
    waveforms: tuple[WaveformData, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    quality_issues: tuple[DataQualityIssue, ...] = ()
    complete: bool = True

    @classmethod
    def create(
        cls,
        sequence: int,
        waveforms: list[WaveformData] | tuple[WaveformData, ...],
        *,
        metadata: dict[str, Any] | None = None,
        required_channels: tuple[str, ...] = (),
        complete: bool = True,
    ) -> "WaveformFrame":
        items = tuple(waveforms)
        return cls(
            sequence=sequence,
            captured_at=datetime.now(timezone.utc).isoformat(),
            waveforms=items,
            metadata=dict(metadata or {}),
            quality_issues=inspect_waveforms(items, required_channels=required_channels),
            complete=complete,
        )


@runtime_checkable
class WaveformSource(Protocol):
    def snapshot(self) -> WaveformFrame: ...


class FileWaveformSource:
    def __init__(self, paths: list[Path] | tuple[Path, ...]) -> None:
        if not paths:
            raise ValueError("至少需要一个波形文件。")
        self.paths = tuple(paths)
        self._sequence = 0

    def snapshot(self) -> WaveformFrame:
        waveforms: list[WaveformData] = []
        for path in self.paths:
            loaded = WaveformData.load_csv_bundle(path)
            waveforms.extend(loaded)
        self._sequence += 1
        return WaveformFrame.create(
            self._sequence,
            waveforms,
            metadata={"source": "file", "paths": [str(path) for path in self.paths]},
        )



@dataclass(frozen=True)
class WaveformViewState:
    x_range: tuple[float, float]
    y_range: tuple[float, float]
    visible_channels: tuple[str, ...] = ()
    waveform_offsets: dict[str, float] = field(default_factory=dict)
    cursor_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    reference_path: str | None = None
    active_tool: str = "ZOOM"
    sidebar_section: str = "测量"
    measurement_overlay_visible: bool = True
    cursor_overlay_visible: bool = True
    theme_name: str = "instrument-dark"


class ViewHistory:
    def __init__(self, capacity: int = 50) -> None:
        self.capacity = max(capacity, 2)
        self._states: list[WaveformViewState] = []
        self._index = -1

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return 0 <= self._index < len(self._states) - 1

    def push(self, state: WaveformViewState) -> None:
        if self._index >= 0 and self._states[self._index] == state:
            return
        del self._states[self._index + 1 :]
        self._states.append(state)
        if len(self._states) > self.capacity:
            self._states.pop(0)
        self._index = len(self._states) - 1

    def undo(self) -> WaveformViewState | None:
        if not self.can_undo:
            return None
        self._index -= 1
        return self._states[self._index]

    def redo(self) -> WaveformViewState | None:
        if not self.can_redo:
            return None
        self._index += 1
        return self._states[self._index]


@dataclass(frozen=True)
class WaveformAnnotation:
    annotation_id: str
    text: str
    start_time_s: float
    end_time_s: float | None = None
    color: str = "#f2994a"
    severity: str = "info"


@dataclass(frozen=True)
class WaveformEvent:
    event_type: str
    channel: str
    time_s: float
    value: float
    description: str


def detect_quick_events(waveforms: tuple[WaveformData, ...] | list[WaveformData]) -> tuple[WaveformEvent, ...]:
    events: list[WaveformEvent] = []
    for waveform in waveforms:
        if not waveform.x_values or not waveform.y_values:
            continue
        minimum_index = min(range(len(waveform.y_values)), key=waveform.y_values.__getitem__)
        maximum_index = max(range(len(waveform.y_values)), key=waveform.y_values.__getitem__)
        events.extend(
            (
                WaveformEvent("minimum", waveform.channel, waveform.x_values[minimum_index],
                              waveform.y_values[minimum_index], "最小值"),
                WaveformEvent("maximum", waveform.channel, waveform.x_values[maximum_index],
                              waveform.y_values[maximum_index], "最大值"),
            )
        )
        stats = waveform.analyze()
        threshold = (stats.logic_low_v + stats.logic_high_v) / 2
        previous = waveform.y_values[0]
        for index, current in enumerate(waveform.y_values[1:], 1):
            if previous < threshold <= current:
                events.append(WaveformEvent("rising", waveform.channel, waveform.x_values[index], current, "上升沿"))
            elif previous > threshold >= current:
                events.append(WaveformEvent("falling", waveform.channel, waveform.x_values[index], current, "下降沿"))
            previous = current
    return tuple(sorted(events, key=lambda event: event.time_s))


def save_workspace_metadata(
    path: Path,
    *,
    bookmarks: dict[str, WaveformViewState],
    annotations: tuple[WaveformAnnotation, ...] | list[WaveformAnnotation],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "bookmarks": {name: asdict(state) for name, state in bookmarks.items()},
        "annotations": [asdict(annotation) for annotation in annotations],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
