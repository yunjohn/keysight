from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from keysight_scope_app.instrument import WaveformData, WaveformStats


class WaveformDetailDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("独立波形分析")
        self.resize(1440, 920)
        layout = QVBoxLayout(self)

        from keysight_scope_app.ui import WaveformAnalysisPanel

        self.analysis_panel = WaveformAnalysisPanel(self, compact_mode=False)
        layout.addWidget(self.analysis_panel)

    def set_waveform(self, waveform: WaveformData, stats: WaveformStats) -> None:
        self.analysis_panel.set_waveform(waveform, stats)

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        self.analysis_panel.set_waveforms(waveforms, primary_stats)

    def clear(self) -> None:
        self.analysis_panel.clear()

    def set_cursor_points(
        self,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        *,
        annotation_text: str | None = None,
    ) -> None:
        self.analysis_panel.set_cursor_points(point_a, point_b, annotation_text=annotation_text)
