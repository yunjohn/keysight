import json
from pathlib import Path

from keysight_scope_app.analysis.waveform import WaveformData, WaveformPreamble
from keysight_scope_app.services.waveform_workspace import (
    FileWaveformSource,
    ViewHistory,
    WaveformAnnotation,
    WaveformViewState,
    detect_quick_events,
    save_workspace_metadata,
)


def _waveform(values: list[float], channel: str = "CHANnel1") -> WaveformData:
    return WaveformData(
        channel,
        "NORMal",
        WaveformPreamble(0, 0, len(values), 1, 0.001, 0, 0, 1, 0, 0),
        [index * 0.001 for index in range(len(values))],
        values,
    )


def _state(start: float) -> WaveformViewState:
    return WaveformViewState((start, start + 1), (-1, 1))


def test_view_history_discards_redo_branch_and_honors_capacity() -> None:
    history = ViewHistory(capacity=3)
    history.push(_state(0))
    history.push(_state(1))
    history.push(_state(2))
    assert history.undo() == _state(1)
    history.push(_state(4))
    assert not history.can_redo
    history.push(_state(5))
    assert history.undo() == _state(4)


def test_file_source_loads_legacy_bundle(tmp_path: Path) -> None:
    path = WaveformData.export_csv_bundle([_waveform([0, 1, 0])], tmp_path / "wave.csv")
    source = FileWaveformSource([path])
    frame = source.snapshot()
    assert frame.sequence == 1
    assert frame.waveforms[0].channel == "CHANnel1"


def test_events_and_workspace_metadata_are_traceable(tmp_path: Path) -> None:
    events = detect_quick_events([_waveform([0, 0, 2, 2, 0])])
    assert {"minimum", "maximum", "rising", "falling"} <= {event.event_type for event in events}
    target = save_workspace_metadata(
        tmp_path / "wave.workspace.json",
        bookmarks={"start": _state(0)},
        annotations=[WaveformAnnotation("a1", "异常", 0.002, severity="warning")],
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["annotations"][0]["text"] == "异常"
