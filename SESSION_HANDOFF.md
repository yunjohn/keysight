# Session Handoff

## Current State

This repository is a PySide6 + PyVISA desktop app for Keysight oscilloscopes. It supports VISA device discovery, connection, screenshots, automatic measurements, waveform capture/export/import, detached waveform analysis, dual cursors, smart lock, pulse/period analysis, multi-channel overlay, channel comparison, and draggable overlay separation.

Main entry points:

- `main.py`
- `src/keysight_scope_app/ui.py`
- `src/keysight_scope_app/instrument.py`
- `tests/test_utils.py`

## Key Features Added In This Session

- Rebuilt UI from Tkinter/Dear PyGui to PySide6.
- Compressed the device connection area and expanded the waveform analysis area.
- Added waveform zoom, X-only/Y-only zoom, cursors A/B, crosshair dragging, edge snap, smart lock, pulse/period lock, detached waveform window.
- Added waveform plot export, CSV export/import, local CSV offline review, and local test guide in `LOCAL_TEST_PLAN.md`.
- Added multi-channel overlay, compare tab, multi-channel bundle CSV import/export, and draggable overlay channel separation.
- Synced imported multi-channel data back to the overlay checkboxes.
- Fixed drag-hit detection for overlay waveforms by switching from point hit testing to interpolated line hit testing.
- Fixed waveform drag so vertical axis scale no longer changes while separating overlay channels.
- Fixed `重置视图` so it resets X/Y ranges and clears waveform separation offsets.
- Fixed cursor rendering so A/B cursor X/Y display is clamped to the current axes and no longer renders outside the plot/title area.

## Local Validation

Run:

```powershell
.venv\Scripts\python.exe -m py_compile src\keysight_scope_app\ui.py
.venv\Scripts\python.exe -m pytest -q
```

Latest result before handoff:

- `13 passed`

## Test Assets Included

- Scope screenshots under `captures/`
- Exported waveform CSVs under `captures/waveforms/`
- Exported waveform images under `captures/waveform_images/`
- UI screenshots: `1.png`, `2.png`, `22.png`

## Suggested Next Work

- Multi-channel local CSV bundle load from arbitrary offline files.
- Annotate waveform plot with more direct measurement overlays.
- Add optional save/restore of waveform separation offsets.
- Add more comparison metrics for propagation delay and phase workflows.
