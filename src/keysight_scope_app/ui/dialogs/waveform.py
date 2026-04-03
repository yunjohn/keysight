from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.analysis.waveform import WaveformData, WaveformStats
from keysight_scope_app.device.instrument import SUPPORTED_CHANNELS
from keysight_scope_app.ui.helpers import display_channel_name
from keysight_scope_app.ui.panels.waveform import WaveformAnalysisPanel
from keysight_scope_app.utils import format_engineering_value


WAVEFORM_CONFIG_DIR = Path("captures") / "waveforms"
WAVEFORM_MEASUREMENT_SETTINGS_PATH = WAVEFORM_CONFIG_DIR / "waveform_measurements.json"
WAVEFORM_MEASUREMENT_ORDER = [
    "频率",
    "周期",
    "脉冲计数",
    "峰峰值",
    "均方根",
    "最大值",
    "最小值",
    "平均值",
    "振幅",
    "占空比",
    "正脉宽",
    "负脉宽",
    "高电平时间",
    "低电平时间",
    "上升时间",
    "下降时间",
    "高电平估计",
    "低电平估计",
]
WAVEFORM_DEFAULT_MEASUREMENTS = {"频率", "峰峰值", "均方根"}
WAVEFORM_CHANNEL_COLORS = {
    "CHANnel1": "#2d9cdb",
    "CHANnel2": "#eb5757",
    "CHANnel3": "#27ae60",
    "CHANnel4": "#f2994a",
}
OVERLAY_TITLE_POINT_SIZE = 10
OVERLAY_BODY_POINT_SIZE = 9
OVERLAY_TITLE_HTML_PX = 14
OVERLAY_BODY_HTML_PX = 12
CURRENT_LIKE_MEASUREMENTS = {
    "峰峰值",
    "均方根",
    "最大值",
    "最小值",
    "平均值",
    "振幅",
    "高电平估计",
    "低电平估计",
}


def _period_from_stats(stats: WaveformStats) -> float | None:
    if stats.estimated_frequency_hz is None or stats.estimated_frequency_hz <= 0:
        return None
    return 1.0 / stats.estimated_frequency_hz


def _negative_pulse_width_from_stats(stats: WaveformStats) -> float | None:
    period_s = _period_from_stats(stats)
    if period_s is None or stats.pulse_width_s is None:
        return None
    return max(period_s - stats.pulse_width_s, 0.0)


def _ratio_to_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100.0


def _measurement_value_from_stats(stats: WaveformStats, measurement_name: str) -> float | None:
    values = {
        "频率": stats.estimated_frequency_hz,
        "周期": _period_from_stats(stats),
        "脉冲计数": float(stats.pulse_count),
        "峰峰值": stats.voltage_pp,
        "均方根": stats.voltage_rms,
        "最大值": stats.voltage_max,
        "最小值": stats.voltage_min,
        "平均值": stats.voltage_mean,
        "振幅": stats.amplitude_v,
        "占空比": _ratio_to_percent(stats.duty_cycle),
        "正脉宽": stats.pulse_width_s,
        "负脉宽": _negative_pulse_width_from_stats(stats),
        "高电平时间": stats.pulse_width_s,
        "低电平时间": _negative_pulse_width_from_stats(stats),
        "上升时间": stats.rise_time_s,
        "下降时间": stats.fall_time_s,
        "高电平估计": stats.logic_high_v,
        "低电平估计": stats.logic_low_v,
    }
    return values.get(measurement_name)


def _measurement_unit(channel_unit: str, measurement_name: str) -> str:
    if measurement_name == "脉冲计数":
        return "个"
    if measurement_name in CURRENT_LIKE_MEASUREMENTS:
        return channel_unit
    if measurement_name == "占空比":
        return "%"
    if measurement_name == "频率":
        return "Hz"
    if measurement_name in {"周期", "正脉宽", "负脉宽", "高电平时间", "低电平时间", "上升时间", "下降时间"}:
        return "s"
    return channel_unit


def _format_measurement_display(value: float | None, unit: str) -> str:
    if value is None:
        return "--"
    if unit == "个":
        return f"{int(round(value))} {unit}"
    return format_engineering_value(value, unit)


class WaveformMeasurementSettingsDialog(QDialog):
    def __init__(
        self,
        channels: list[str],
        measurement_config: dict[str, set[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("测量项设置")
        self.resize(780, 520)
        self._channels = channels
        self.channel_checks: dict[str, dict[str, QCheckBox]] = {}

        layout = QVBoxLayout(self)
        hint = QLabel("这些测量项只作用于当前独立波形显示窗口，基于已加载波形计算。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tabs = QTabWidget(self)
        for channel in channels:
            tab = QWidget(self)
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(8, 8, 8, 8)
            tab_layout.setSpacing(8)

            top_row = QHBoxLayout()
            top_row.addWidget(QLabel(f"{display_channel_name(channel)} 测量项"))
            sync_button = QPushButton("同步到其它通道")
            sync_button.clicked.connect(lambda checked=False, source=channel: self._sync_to_other_channels(source))
            top_row.addStretch(1)
            top_row.addWidget(sync_button)
            tab_layout.addLayout(top_row)

            checks_layout = QGridLayout()
            checks_layout.setHorizontalSpacing(18)
            checks_layout.setVerticalSpacing(8)
            channel_check_map: dict[str, QCheckBox] = {}
            selected = measurement_config.get(channel, set(WAVEFORM_DEFAULT_MEASUREMENTS))
            for index, name in enumerate(WAVEFORM_MEASUREMENT_ORDER):
                checkbox = QCheckBox(name)
                checkbox.setChecked(name in selected)
                channel_check_map[name] = checkbox
                checks_layout.addWidget(checkbox, index // 3, index % 3)
            self.channel_checks[channel] = channel_check_map
            tab_layout.addLayout(checks_layout)
            tab_layout.addStretch(1)
            self.tabs.addTab(tab, display_channel_name(channel))
        layout.addWidget(self.tabs, 1)

        button_row = QHBoxLayout()
        self.reset_button = QPushButton("恢复默认")
        self.cancel_button = QPushButton("取消")
        self.ok_button = QPushButton("确定")
        self.reset_button.clicked.connect(self._reset_current_channel)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.ok_button)
        layout.addLayout(button_row)

    def selected_measurements(self) -> dict[str, set[str]]:
        return {
            channel: {name for name, checkbox in checks.items() if checkbox.isChecked()}
            for channel, checks in self.channel_checks.items()
        }

    def _current_channel(self) -> str:
        index = self.tabs.currentIndex()
        if index < 0 or index >= len(self._channels):
            return self._channels[0]
        return self._channels[index]

    def _reset_current_channel(self) -> None:
        checks = self.channel_checks[self._current_channel()]
        for name, checkbox in checks.items():
            checkbox.setChecked(name in WAVEFORM_DEFAULT_MEASUREMENTS)

    def _sync_to_other_channels(self, source_channel: str) -> None:
        selected = {
            name
            for name, checkbox in self.channel_checks[source_channel].items()
            if checkbox.isChecked()
        }
        for channel, checks in self.channel_checks.items():
            if channel == source_channel:
                continue
            for name, checkbox in checks.items():
                checkbox.setChecked(name in selected)


class WaveformDetailDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.setWindowTitle("独立波形显示")
        self.resize(1440, 920)
        layout = QVBoxLayout(self)
        self.channel_visibility_checks: dict[str, QCheckBox] = {}
        self.current_waveforms: list[WaveformData] = []
        self.measurement_config: dict[str, set[str]] = {}
        self.cursor_measurements: dict[str, str] = {}
        self._updating_channel_checks = False
        self._load_measurement_config()

        toolbar = QHBoxLayout()
        self.refresh_waveform_button = QPushButton("抓取波形")
        self.refresh_waveform_button.clicked.connect(self._request_waveform_refresh)
        self.reset_waveform_button = QPushButton("重置波形")
        self.reset_waveform_button.clicked.connect(self._reset_waveform_view)
        self.measurement_scope_combo = QComboBox()
        self.measurement_scope_combo.addItem("当前视图", "view")
        self.measurement_scope_combo.addItem("游标 A-B", "cursor")
        self.measurement_scope_combo.addItem("整条波形", "full")
        self.measurement_scope_combo.currentIndexChanged.connect(self._refresh_measurement_footer)
        self.measurement_settings_button = QPushButton("测量项设置")
        self.measurement_settings_button.clicked.connect(self._show_measurement_settings)
        toolbar.addWidget(self.refresh_waveform_button)
        toolbar.addWidget(self.reset_waveform_button)
        toolbar.addWidget(self.measurement_settings_button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("测量范围"))
        toolbar.addWidget(self.measurement_scope_combo)
        layout.addLayout(toolbar)

        self.operation_hint_label = QLabel(
            "左键框选放大时间轴，Shift+左键拖动平移，滚轮双轴缩放，Shift+滚轮缩放时间轴，Ctrl+滚轮缩放幅值，右键管理游标。"
        )
        self.operation_hint_label.setWordWrap(True)
        self.operation_hint_label.setStyleSheet("color: #5f6b76;")
        hint_font = self.operation_hint_label.font()
        hint_font.setPointSize(max(hint_font.pointSize() - 1, 9))
        self.operation_hint_label.setFont(hint_font)
        layout.addWidget(self.operation_hint_label)

        self.analysis_panel = WaveformAnalysisPanel(self, compact_mode=False)
        self.analysis_panel.channel_unit_resolver = self._channel_unit
        self.analysis_panel.cursor_readout_changed = self._handle_cursor_measurements_changed
        self.analysis_panel.view_window_changed = self._refresh_measurement_footer
        self.analysis_panel.set_waveform_only_mode(True)
        layout.addWidget(self.analysis_panel)

        self.channel_toggle_container = QWidget(self.analysis_panel)
        self.channel_toggle_layout = QHBoxLayout(self.channel_toggle_container)
        self.channel_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_toggle_layout.setSpacing(8)
        self.analysis_panel.layout().insertWidget(2, self.channel_toggle_container)

        self.measurement_overlay = QFrame(self.analysis_panel.chart_view)
        self.measurement_overlay.setObjectName("measurementOverlay")
        self.measurement_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.measurement_overlay.setFrameShape(QFrame.StyledPanel)
        self.measurement_overlay.setStyleSheet(
            "#measurementOverlay { background-color: transparent; border: 0; }"
            "#measurementCard { background-color: transparent; border: 0; }"
        )
        overlay_layout = QVBoxLayout(self.measurement_overlay)
        overlay_layout.setContentsMargins(6, 4, 6, 4)
        overlay_layout.setSpacing(3)
        self.measurement_overlay_hint = QLabel("波形测量数据会显示在这里。")
        self.measurement_overlay_hint.setWordWrap(True)
        hint_font = self.measurement_overlay_hint.font()
        hint_font.setPointSize(OVERLAY_TITLE_POINT_SIZE)
        self.measurement_overlay_hint.setFont(hint_font)
        self.measurement_overlay_hint.setStyleSheet("color: rgba(40, 40, 40, 180);")
        overlay_layout.addWidget(self.measurement_overlay_hint)

        self.measurement_text_label = QLabel(self.measurement_overlay)
        self.measurement_text_label.setWordWrap(True)
        self.measurement_text_label.setTextFormat(Qt.RichText)
        self.measurement_text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_font = self.measurement_text_label.font()
        text_font.setPointSize(OVERLAY_BODY_POINT_SIZE)
        self.measurement_text_label.setFont(text_font)
        overlay_layout.addWidget(self.measurement_text_label)
        overlay_layout.setSizeConstraint(QVBoxLayout.SetMinimumSize)
        self.measurement_overlay.hide()

        self.cursor_overlay = QFrame(self.analysis_panel.chart_view)
        self.cursor_overlay.setObjectName("cursorOverlay")
        self.cursor_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.cursor_overlay.setFrameShape(QFrame.StyledPanel)
        self.cursor_overlay.setStyleSheet("#cursorOverlay { background-color: transparent; border: 0; }")
        cursor_layout = QVBoxLayout(self.cursor_overlay)
        cursor_layout.setContentsMargins(4, 3, 4, 3)
        cursor_layout.setSpacing(3)
        self.cursor_overlay_hint = QLabel("■ 游标测量")
        cursor_hint_font = self.cursor_overlay_hint.font()
        cursor_hint_font.setPointSize(OVERLAY_TITLE_POINT_SIZE)
        cursor_hint_font.setBold(True)
        self.cursor_overlay_hint.setFont(cursor_hint_font)
        self.cursor_overlay_hint.setStyleSheet("color: #404040; letter-spacing: 0.4px;")
        cursor_layout.addWidget(self.cursor_overlay_hint)
        self.cursor_text_label = QLabel(self.cursor_overlay)
        self.cursor_text_label.setWordWrap(True)
        self.cursor_text_label.setTextFormat(Qt.RichText)
        self.cursor_text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cursor_text_label.setFont(text_font)
        cursor_layout.addWidget(self.cursor_text_label)
        cursor_layout.setSizeConstraint(QVBoxLayout.SetMinimumSize)
        self.cursor_overlay.hide()
        self.analysis_panel.chart_view.installEventFilter(self)

    def set_waveform(self, waveform: WaveformData, stats: WaveformStats) -> None:
        self.current_waveforms = [waveform]
        self.analysis_panel.set_waveform(waveform, stats)
        self._ensure_measurement_defaults([waveform])
        self._refresh_measurement_footer()
        self._rebuild_channel_visibility_checks([waveform])

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        self.current_waveforms = list(waveforms)
        self.analysis_panel.set_waveforms(waveforms, primary_stats)
        self._ensure_measurement_defaults(waveforms)
        self._refresh_measurement_footer()
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
        self.current_waveforms = []
        self.cursor_measurements = {}
        self.analysis_panel.clear()
        self._refresh_measurement_footer()
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
        parent = self.parent()
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
        active_waveform_channels = {waveform.channel for waveform in waveforms}
        for channel in SUPPORTED_CHANNELS:
            checkbox = QCheckBox(display_channel_name(channel))
            if parent is not None and hasattr(parent, "scope_display_checks"):
                parent_checks = getattr(parent, "scope_display_checks", {})
                parent_checkbox = parent_checks.get(channel)
                checked = parent_checkbox.isChecked() if parent_checkbox is not None else previous_states.get(channel, channel in active_waveform_channels)
            else:
                checked = previous_states.get(channel, channel in active_waveform_channels)
            checkbox.setChecked(checked)
            checkbox.toggled.connect(
                lambda checked=False, target_channel=channel: self._handle_channel_checkbox_toggled(target_channel, checked)
            )
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
        self._refresh_measurement_footer()

    def _channel_unit(self, channel: str) -> str:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_channel_unit"):
            return parent._channel_unit(channel)
        return "V"

    def _request_waveform_refresh(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "refresh_waveform_detail_dialog"):
            parent.refresh_waveform_detail_dialog()

    def _handle_channel_checkbox_toggled(self, channel: str, checked: bool) -> None:
        if self._updating_channel_checks:
            self._apply_channel_visibility()
            return
        parent = self.parent()
        if parent is not None and hasattr(parent, "request_scope_channel_display_from_detail_dialog"):
            parent.request_scope_channel_display_from_detail_dialog(channel, checked)
            return
        self._apply_channel_visibility()

    def sync_scope_channel_checks(self, channels: list[str]) -> None:
        active_channels = set(channels)
        self._updating_channel_checks = True
        try:
            for channel, checkbox in self.channel_visibility_checks.items():
                checkbox.setChecked(channel in active_channels)
        finally:
            self._updating_channel_checks = False
        self._apply_channel_visibility()

    def _reset_waveform_view(self) -> None:
        self.analysis_panel.reset_view()

    def _show_measurement_settings(self) -> None:
        if not self.current_waveforms:
            self.measurement_overlay_hint.setText("请先抓取或加载波形，再设置测量项。")
            return
        channels = [waveform.channel for waveform in self.current_waveforms]
        dialog = WaveformMeasurementSettingsDialog(channels, self.measurement_config, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.measurement_config = dialog.selected_measurements()
        self._save_measurement_config()
        self._refresh_measurement_footer()

    def _handle_cursor_measurements_changed(self, measurements: dict[str, str]) -> None:
        self.cursor_measurements = measurements
        self._refresh_measurement_footer()

    def _ensure_measurement_defaults(self, waveforms: list[WaveformData]) -> None:
        for waveform in waveforms:
            self.measurement_config.setdefault(waveform.channel, set(WAVEFORM_DEFAULT_MEASUREMENTS))

    def _save_measurement_config(self) -> None:
        WAVEFORM_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "measurement_config": {
                channel: sorted(
                    name for name in selected if name in WAVEFORM_MEASUREMENT_ORDER
                )
                for channel, selected in self.measurement_config.items()
            }
        }
        with WAVEFORM_MEASUREMENT_SETTINGS_PATH.open("w", encoding="utf-8") as settings_file:
            json.dump(payload, settings_file, ensure_ascii=False, indent=2)

    def _load_measurement_config(self) -> None:
        if not WAVEFORM_MEASUREMENT_SETTINGS_PATH.exists():
            return
        try:
            payload = json.loads(WAVEFORM_MEASUREMENT_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log_message(f"波形测量项配置加载失败: {exc}")
            return

        loaded_config: dict[str, set[str]] = {}
        for channel, selected in payload.get("measurement_config", {}).items():
            valid_names = {
                str(name)
                for name in selected
                if str(name) in WAVEFORM_MEASUREMENT_ORDER
            }
            if valid_names:
                loaded_config[str(channel)] = valid_names
        self.measurement_config = loaded_config

    def _log_message(self, message: str) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "log"):
            parent.log(message)

    def _refresh_measurement_footer(self) -> None:
        if not self.current_waveforms:
            self.measurement_overlay_hint.clear()
            self.measurement_text_label.clear()
            self.measurement_overlay.hide()
            self.cursor_text_label.clear()
            self.cursor_overlay.hide()
            return

        channel_sections: list[str] = []
        measurement_scope = self._selected_measurement_scope()
        for waveform in self.current_waveforms:
            section_html = self._build_measurement_section_html(waveform, measurement_scope)
            if section_html:
                channel_sections.append(section_html)

        cursor_section = self._build_cursor_measurement_section_html()

        hint_text = ""
        if measurement_scope == "cursor" and self.analysis_panel.cursor_time_window() is None:
            hint_text = "当前测量范围：游标 A-B。请先放置两根游标。"
        elif measurement_scope == "cursor":
            hint_text = "当前测量范围：游标 A-B。"
        elif measurement_scope == "full":
            hint_text = "当前测量范围：整条波形。"
        else:
            hint_text = "当前测量范围：当前视图。"

        if not channel_sections and not cursor_section:
            self.measurement_overlay_hint.setText(hint_text if hint_text else "")
            self.measurement_text_label.clear()
            self.measurement_overlay.hide()
            self.cursor_text_label.clear()
            self.cursor_overlay.hide()
        else:
            if channel_sections:
                self.measurement_overlay_hint.setText(hint_text)
                self.measurement_text_label.setText(self._build_measurement_overlay_html(channel_sections))
                self.measurement_overlay.show()
            else:
                self.measurement_text_label.clear()
                if hint_text:
                    self.measurement_overlay_hint.setText(hint_text)
                    self.measurement_overlay.show()
                else:
                    self.measurement_overlay.hide()

            if cursor_section:
                self.cursor_overlay_hint.setText("游标测量")
                self.cursor_text_label.setText(cursor_section)
                self.cursor_overlay.show()
            else:
                self.cursor_text_label.clear()
                self.cursor_overlay.hide()

            self._reposition_measurement_overlay()

    def _selected_measurement_scope(self) -> str:
        return str(self.measurement_scope_combo.currentData())

    def _measurement_stats_for_channel(self, channel: str, measurement_scope: str) -> WaveformStats | None:
        if measurement_scope == "cursor":
            return self.analysis_panel.cursor_window_stats_for_channel(channel)
        if measurement_scope == "full":
            return self.analysis_panel.full_stats_for_channel(channel)
        return self.analysis_panel.visible_stats_for_channel(channel)

    def _build_measurement_section_html(self, waveform: WaveformData, measurement_scope: str) -> str:
        stats = self._measurement_stats_for_channel(waveform.channel, measurement_scope)
        if stats is None:
            return ""
        channel_unit = self._channel_unit(waveform.channel)
        selected_names = self.measurement_config.get(waveform.channel, set(WAVEFORM_DEFAULT_MEASUREMENTS))
        metric_items: list[str] = []
        for measurement_name in WAVEFORM_MEASUREMENT_ORDER:
            if measurement_name not in selected_names:
                continue
            raw_value = _measurement_value_from_stats(stats, measurement_name)
            unit = _measurement_unit(channel_unit, measurement_name)
            formatted_value = _format_measurement_display(raw_value, unit)
            metric_items.append(
                f"<span style='color:{WAVEFORM_CHANNEL_COLORS.get(waveform.channel, '#222222')};'>"
                f"{measurement_name}"
                "</span>"
                f"<span style='color:{WAVEFORM_CHANNEL_COLORS.get(waveform.channel, '#222222')};'>: </span>"
                f"<span style='font-weight:600; color:{WAVEFORM_CHANNEL_COLORS.get(waveform.channel, '#222222')};'>"
                f"{formatted_value}"
                "</span>"
            )

        if not metric_items:
            return ""

        title = display_channel_name(waveform.channel)
        title_color = WAVEFORM_CHANNEL_COLORS.get(waveform.channel, "#222222")
        rows: list[str] = []
        items_per_column = 4
        column_count = max((len(metric_items) + items_per_column - 1) // items_per_column, 1)
        for row_index in range(items_per_column):
            row_items = []
            for column_index in range(column_count):
                item_index = column_index * items_per_column + row_index
                if item_index < len(metric_items):
                    row_items.append(metric_items[item_index])
            if not row_items:
                continue
            rows.append(
                "<tr>"
                + "".join(
                    f"<td style='padding:0 18px 8px 0; vertical-align:top; white-space:nowrap; line-height:1.62; font-size:{OVERLAY_BODY_HTML_PX}px;'>{item}</td>"
                    for item in row_items
                )
                + "</tr>"
            )
        return (
            f"<div style='margin-bottom:0;'>"
            f"<div style='font-weight:700; color:{title_color}; margin-bottom:4px; text-align:left; "
            f"letter-spacing:0.4px; font-size:{OVERLAY_TITLE_HTML_PX}px;'>"
            f"<span style='font-size:{OVERLAY_TITLE_HTML_PX}px;'>&#9632;</span> {title}"
            f"</div>"
            f"<table cellspacing='0' cellpadding='0' width='100%' style='margin-left:6px;'>{''.join(rows)}</table>"
            f"</div>"
        )

    def _build_measurement_overlay_html(self, channel_sections: list[str]) -> str:
        if not channel_sections:
            return ""
        column_count = max(len(channel_sections), 1)
        column_width = 100.0 / column_count
        return (
            "<table cellspacing='0' cellpadding='0' width='100%'>"
            "<tr>"
            + "".join(
                f"<td width='{column_width:.2f}%' style='vertical-align:top; padding:0 18px; white-space:nowrap;' align='left'>{section}</td>"
                for section in channel_sections
            )
            + "</tr>"
            + "</table>"
        )

    def _build_cursor_measurement_section_html(self) -> str:
        if not self.cursor_measurements:
            return ""
        visible_items = [
            (label, value)
            for label, value in self.cursor_measurements.items()
            if value and value != "-"
        ]
        if not visible_items:
            return ""

        rows: list[str] = []
        for label, value in visible_items:
            rows.append(
                "<tr>"
                f"<td style='padding:0 0 10px 0; vertical-align:top; white-space:nowrap; line-height:1.62; font-size:{OVERLAY_BODY_HTML_PX}px;'>"
                f"<span style='color:rgba(45,45,45,0.72);'>{label}</span>"
                f"<br><span style='font-weight:600; color:#1f1f1f;'>{value}</span>"
                "</td>"
                "</tr>"
            )
        return (
            "<table cellspacing='0' cellpadding='0'>"
            + "".join(rows)
            + "</table>"
        )

    def _reposition_measurement_overlay(self) -> None:
        chart_rect = self.analysis_panel.chart_view.rect()
        if self.measurement_overlay.isVisible():
            overlay_width = min(max(int(chart_rect.width() * 0.86), 900), chart_rect.width() - 44)
            self.measurement_overlay.setFixedWidth(overlay_width)
            self.measurement_text_label.setFixedWidth(max(overlay_width - 16, 240))
            self.measurement_text_label.adjustSize()
            content_height = self.measurement_text_label.sizeHint().height()
            hint_height = self.measurement_overlay_hint.sizeHint().height()
            overlay_height = hint_height + content_height + 18
            x_pos = max((chart_rect.width() - overlay_width) // 2 + 80, 12)
            y_pos = max(chart_rect.height() - 260, 12)
            self.measurement_overlay.setGeometry(x_pos, y_pos, overlay_width, overlay_height)
            self.measurement_overlay.raise_()

        if self.cursor_overlay.isVisible():
            cursor_width = min(max(int(chart_rect.width() * 0.17), 220), 290)
            self.cursor_overlay.setFixedWidth(cursor_width)
            self.cursor_text_label.setFixedWidth(max(cursor_width - 10, 140))
            self.cursor_text_label.adjustSize()
            cursor_content_height = self.cursor_text_label.sizeHint().height()
            cursor_hint_height = self.cursor_overlay_hint.sizeHint().height()
            cursor_height = cursor_hint_height + cursor_content_height + 12
            cursor_x = max(chart_rect.width() - cursor_width - 18, 12)
            cursor_y = 24
            self.cursor_overlay.setGeometry(cursor_x, cursor_y, cursor_width, cursor_height)
            self.cursor_overlay.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._reposition_measurement_overlay()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.analysis_panel.chart_view and event.type() in {QEvent.Resize, QEvent.Show}:
            self._reposition_measurement_overlay()
        return super().eventFilter(watched, event)


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
