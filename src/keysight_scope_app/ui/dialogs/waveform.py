from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from keysight_scope_app.analysis.waveform import WaveformData, WaveformStats
from keysight_scope_app.ui.helpers import display_channel_name
from keysight_scope_app.ui.panels.waveform import WaveformAnalysisPanel


class WaveformDetailDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.setWindowTitle("独立波形分析")
        self.resize(1440, 920)
        layout = QVBoxLayout(self)
        self.channel_visibility_checks: dict[str, QCheckBox] = {}

        toolbar = QHBoxLayout()
        self.refresh_waveform_button = QPushButton("抓取波形")
        self.refresh_waveform_button.clicked.connect(self._request_waveform_refresh)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_waveform_button)
        layout.addLayout(toolbar)

        self.analysis_panel = WaveformAnalysisPanel(self, compact_mode=False)
        self.analysis_panel.channel_unit_resolver = self._channel_unit
        layout.addWidget(self.analysis_panel)

        self.channel_toggle_container = QWidget(self.analysis_panel)
        self.channel_toggle_layout = QHBoxLayout(self.channel_toggle_container)
        self.channel_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_toggle_layout.setSpacing(8)
        self.analysis_panel.layout().insertWidget(2, self.channel_toggle_container)

    def set_waveform(self, waveform: WaveformData, stats: WaveformStats) -> None:
        self.analysis_panel.set_waveform(waveform, stats)
        self._rebuild_channel_visibility_checks([waveform])

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        self.analysis_panel.set_waveforms(waveforms, primary_stats)
        self._rebuild_channel_visibility_checks(waveforms)

    def set_timebase_scale(self, seconds_per_div: float, *, divisions: int = 10) -> None:
        self.analysis_panel.set_timebase_scale(seconds_per_div, divisions=divisions)

    def set_scope_vertical_layouts(self, layouts: dict[str, dict[str, float]]) -> None:
        self.analysis_panel.set_scope_vertical_layouts(layouts)

    def focus_on_point(self, point: tuple[float, float], *, annotation_text: str | None = None) -> None:
        self.analysis_panel.focus_on_point(point, annotation_text=annotation_text)

    def focus_on_channel_point(
        self,
        point: tuple[float, float],
        *,
        channel: str | None,
        annotation_text: str | None = None,
    ) -> None:
        self.analysis_panel.focus_on_channel_point(point, channel=channel, annotation_text=annotation_text)

    def clear(self) -> None:
        self.analysis_panel.clear()
        self._rebuild_channel_visibility_checks([])

    def set_cursor_points(
        self,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        *,
        annotation_text: str | None = None,
    ) -> None:
        self.analysis_panel.set_cursor_points(point_a, point_b, annotation_text=annotation_text)

    def _rebuild_channel_visibility_checks(self, waveforms: list[WaveformData]) -> None:
        previous_states = {
            channel: checkbox.isChecked()
            for channel, checkbox in self.channel_visibility_checks.items()
        }
        while self.channel_toggle_layout.count():
            item = self.channel_toggle_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.channel_toggle_layout.addStretch(1)
        self.channel_toggle_layout.addWidget(QLabel("显示通道"))
        self.channel_visibility_checks = {}
        for waveform in waveforms:
            channel = waveform.channel
            checkbox = QCheckBox(display_channel_name(channel))
            checkbox.setChecked(previous_states.get(channel, True))
            checkbox.toggled.connect(lambda checked=False: self._apply_channel_visibility())
            self.channel_visibility_checks[channel] = checkbox
            self.channel_toggle_layout.addWidget(checkbox)
        self.channel_toggle_layout.addStretch(1)
        self._apply_channel_visibility()

    def _apply_channel_visibility(self) -> None:
        visible_channels = {
            channel
            for channel, checkbox in self.channel_visibility_checks.items()
            if checkbox.isChecked()
        }
        self.analysis_panel.set_visible_channels(visible_channels)

    def _channel_unit(self, channel: str) -> str:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_channel_unit"):
            return parent._channel_unit(channel)
        return "V"

    def _request_waveform_refresh(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "refresh_waveform_detail_dialog"):
            parent.refresh_waveform_detail_dialog()


class WaveformOnlyDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.setWindowTitle("独立波形显示")
        self.resize(1440, 920)
        layout = QVBoxLayout(self)
        self.channel_visibility_checks: dict[str, QCheckBox] = {}

        toolbar = QHBoxLayout()
        self.refresh_waveform_button = QPushButton("抓取波形")
        self.refresh_waveform_button.clicked.connect(self._request_waveform_refresh)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_waveform_button)
        layout.addLayout(toolbar)

        self.analysis_panel = WaveformAnalysisPanel(self, compact_mode=False)
        self.analysis_panel.channel_unit_resolver = self._channel_unit
        self.analysis_panel.set_waveform_only_mode(True)
        layout.addWidget(self.analysis_panel)

        channel_bar = QWidget(self)
        self.channel_toggle_layout = QHBoxLayout(channel_bar)
        self.channel_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_toggle_layout.setSpacing(8)
        layout.addWidget(channel_bar)

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        self.analysis_panel.set_waveforms(waveforms, primary_stats)
        self._rebuild_channel_visibility_checks(waveforms)

    def set_scope_vertical_layouts(self, layouts: dict[str, dict[str, float]]) -> None:
        self.analysis_panel.set_scope_vertical_layouts(layouts)

    def clear(self) -> None:
        self.analysis_panel.clear()
        self._rebuild_channel_visibility_checks([])

    def _rebuild_channel_visibility_checks(self, waveforms: list[WaveformData]) -> None:
        previous_states = {
            channel: checkbox.isChecked()
            for channel, checkbox in self.channel_visibility_checks.items()
        }
        while self.channel_toggle_layout.count():
            item = self.channel_toggle_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.channel_toggle_layout.addStretch(1)
        self.channel_toggle_layout.addWidget(QLabel("显示通道"))
        self.channel_visibility_checks = {}
        for waveform in waveforms:
            channel = waveform.channel
            checkbox = QCheckBox(display_channel_name(channel))
            checkbox.setChecked(previous_states.get(channel, True))
            checkbox.toggled.connect(lambda checked=False: self._apply_channel_visibility())
            self.channel_visibility_checks[channel] = checkbox
            self.channel_toggle_layout.addWidget(checkbox)
        self.channel_toggle_layout.addStretch(1)
        self._apply_channel_visibility()

    def _apply_channel_visibility(self) -> None:
        visible_channels = {
            channel
            for channel, checkbox in self.channel_visibility_checks.items()
            if checkbox.isChecked()
        }
        self.analysis_panel.set_visible_channels(visible_channels)

    def _channel_unit(self, channel: str) -> str:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_channel_unit"):
            return parent._channel_unit(channel)
        return "V"

    def _request_waveform_refresh(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "refresh_waveform_only_dialog"):
            parent.refresh_waveform_only_dialog()

    def _reset_view(self) -> None:
        self.analysis_panel.reset_view()
