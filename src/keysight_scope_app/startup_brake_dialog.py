from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.instrument import SUPPORTED_CHANNELS
from keysight_scope_app.startup_brake_analysis import (
    StartupBrakeTestConfig,
    StartupBrakeTestResult,
    analyze_startup_brake_test,
)
from keysight_scope_app.waveform_analysis import WaveformData

if TYPE_CHECKING:
    from keysight_scope_app.ui import ScopeMainWindow


STARTUP_BRAKE_DIR = Path("captures") / "startup_brake_tests"


def _display_channel_name(channel: str) -> str:
    if channel.startswith("CHANnel"):
        return channel.replace("CHANnel", "CH", 1)
    return channel


def _normalize_channel_name(channel: str) -> str:
    normalized = channel.strip()
    if normalized.upper().startswith("CH") and normalized[2:].isdigit():
        return f"CHANnel{normalized[2:]}"
    return normalized


def _format_peak_current(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.value:.6f} A"


def _format_peak_time(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.time_s:.6e} s"


def _format_range_ms(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.3f} ~ {max(values):.3f} ms"


def _format_range_amp(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} A"


def _format_range_hz(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} Hz"


@dataclass(frozen=True)
class StartupBrakeHistoryEntry:
    result: StartupBrakeTestResult
    timestamp: str
    config: StartupBrakeTestConfig


class StartupBrakeTestDialog(QDialog):
    DEFAULT_SUMMARY_TEXT = "提示：执行测试时会优先复用当前波形；缺少通道时会按当前波形采样参数补抓。"

    def __init__(self, main_window: ScopeMainWindow) -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.last_result: StartupBrakeTestResult | None = None
        self.history: list[StartupBrakeHistoryEntry] = []
        self.channel_previous: dict[int, str] = {}

        self.setWindowTitle("启动刹车性能测试")
        self.resize(1180, 760)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_test_box())

        for combo in self.channel_combos:
            combo.currentIndexChanged.connect(
                lambda _, changed_combo=combo: self._refresh_channel_options(changed_combo)
            )
        self.target_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_target_fields())
        self.target_value_input.valueChanged.connect(lambda _: self._refresh_target_fields())
        self.ppr_input.valueChanged.connect(lambda _: self._refresh_target_fields())
        self.brake_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_mode_fields())
        self.run_button.clicked.connect(self.run_test)
        self.apply_startup_cursor_button.clicked.connect(self._apply_startup_cursors)
        self.apply_brake_cursor_button.clicked.connect(self._apply_brake_cursors)
        self.export_stats_button.clicked.connect(self._export_history_csv)
        self.clear_stats_button.clicked.connect(self.clear_history)

        self._stabilize_push_buttons(self)
        self._normalize_label_alignment(self)
        self.clear_results()
        self._refresh_history()
        self._refresh_channel_options()
        self._refresh_target_fields()
        self._refresh_mode_fields()

    def show_dialog(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def handle_waveforms_updated(self) -> None:
        self.last_result = None
        self.clear_results()

    def reset_state(self) -> None:
        self.last_result = None
        self.clear_results()
        self.clear_history()

    def _group_box(self, title: str) -> QGroupBox:
        return QGroupBox(title)

    def _build_test_box(self) -> QGroupBox:
        box = self._group_box("启动刹车性能测试")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        channel_title = QLabel("通道配置")
        channel_title.setFont(QFont(channel_title.font().family(), channel_title.font().pointSize(), QFont.Bold))
        layout.addWidget(channel_title)

        self.control_channel_combo = self._create_channel_combo("CHANnel1")
        self.speed_channel_combo = self._create_channel_combo("CHANnel2")
        self.current_channel_combo = self._create_channel_combo("CHANnel3")
        self.encoder_channel_combo = self._create_channel_combo("CHANnel4")
        self.channel_combos = [
            self.control_channel_combo,
            self.speed_channel_combo,
            self.current_channel_combo,
            self.encoder_channel_combo,
        ]
        self.channel_previous = {
            id(combo): self._selected_channel_from_combo(combo) for combo in self.channel_combos
        }
        self._set_compact_field_width(
            self.control_channel_combo,
            self.speed_channel_combo,
            self.current_channel_combo,
            self.encoder_channel_combo,
        )
        channel_grid = QGridLayout()
        channel_grid.setHorizontalSpacing(12)
        channel_grid.setVerticalSpacing(6)
        channel_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        channel_grid.addWidget(self._inline_form_field("控制输入", self.control_channel_combo), 0, 0)
        channel_grid.addWidget(self._inline_form_field("转速反馈", self.speed_channel_combo), 0, 1)
        channel_grid.addWidget(self._inline_form_field("电流通道", self.current_channel_combo), 1, 0)
        self.encoder_field = self._inline_form_field("编码器 A 相", self.encoder_channel_combo)
        channel_grid.addWidget(self.encoder_field, 1, 1)
        layout.addLayout(channel_grid)

        speed_title = QLabel("达速判定")
        speed_title.setFont(QFont(speed_title.font().family(), speed_title.font().pointSize(), QFont.Bold))
        layout.addWidget(speed_title)

        self.target_mode_combo = QComboBox()
        self.target_mode_combo.addItem("频率(Hz)", "frequency_hz")
        self.target_mode_combo.addItem("周期(ms)", "period_ms")
        self.target_mode_combo.addItem("转速(RPM)", "rpm")
        self.target_value_input = QDoubleSpinBox()
        self.target_value_input.setDecimals(3)
        self.target_value_input.setRange(0.001, 1_000_000.0)
        self.target_value_input.setValue(100.0)
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setDecimals(2)
        self.tolerance_input.setSuffix(" %")
        self.tolerance_input.setRange(0.0, 100.0)
        self.tolerance_input.setValue(5.0)
        self.consecutive_input = QDoubleSpinBox()
        self.consecutive_input.setDecimals(0)
        self.consecutive_input.setRange(1, 20)
        self.consecutive_input.setValue(3)
        self.ppr_input = QDoubleSpinBox()
        self.ppr_input.setDecimals(0)
        self.ppr_input.setRange(1, 100000)
        self.ppr_input.setValue(1)
        self._set_compact_field_width(
            self.target_mode_combo,
            self.target_value_input,
            self.tolerance_input,
            self.consecutive_input,
            self.ppr_input,
        )
        speed_grid = QGridLayout()
        speed_grid.setHorizontalSpacing(12)
        speed_grid.setVerticalSpacing(6)
        speed_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        speed_grid.addWidget(self._inline_form_field("目标类型", self.target_mode_combo), 0, 0)
        speed_grid.addWidget(self._inline_form_field("目标值", self.target_value_input), 0, 1)
        speed_grid.addWidget(self._inline_form_field("容差", self.tolerance_input), 0, 2)
        speed_grid.addWidget(self._inline_form_field("连续周期", self.consecutive_input), 1, 0)
        self.ppr_field = self._inline_form_field("每转脉冲数", self.ppr_input)
        speed_grid.addWidget(self.ppr_field, 1, 1)
        layout.addLayout(speed_grid)

        self.target_hint_label = QLabel("")
        self.target_hint_label.setWordWrap(True)
        layout.addWidget(self.target_hint_label)

        brake_title = QLabel("刹车判定")
        brake_title.setFont(QFont(brake_title.font().family(), brake_title.font().pointSize(), QFont.Bold))
        layout.addWidget(brake_title)

        self.brake_mode_combo = QComboBox()
        self.brake_mode_combo.addItem("电流归零", "current_zero")
        self.brake_mode_combo.addItem("A相回溯", "encoder_backtrack")
        self.zero_threshold_input = QDoubleSpinBox()
        self.zero_threshold_input.setDecimals(3)
        self.zero_threshold_input.setRange(0.0, 1000.0)
        self.zero_threshold_input.setValue(0.05)
        self.flat_threshold_input = QDoubleSpinBox()
        self.flat_threshold_input.setDecimals(3)
        self.flat_threshold_input.setRange(0.0, 1000.0)
        self.flat_threshold_input.setValue(0.03)
        self.hold_ms_input = QDoubleSpinBox()
        self.hold_ms_input.setDecimals(3)
        self.hold_ms_input.setSuffix(" ms")
        self.hold_ms_input.setRange(0.0, 1000.0)
        self.hold_ms_input.setValue(2.0)
        self.backtrack_pulses_input = QDoubleSpinBox()
        self.backtrack_pulses_input.setDecimals(0)
        self.backtrack_pulses_input.setRange(1, 1000)
        self.backtrack_pulses_input.setValue(8)
        self._set_compact_field_width(
            self.brake_mode_combo,
            self.zero_threshold_input,
            self.flat_threshold_input,
            self.hold_ms_input,
            self.backtrack_pulses_input,
        )
        brake_grid = QGridLayout()
        brake_grid.setHorizontalSpacing(12)
        brake_grid.setVerticalSpacing(6)
        brake_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        brake_grid.addWidget(self._inline_form_field("刹车模式", self.brake_mode_combo), 0, 0)
        brake_grid.addWidget(self._inline_form_field("零电流阈值", self.zero_threshold_input), 0, 1)
        brake_grid.addWidget(self._inline_form_field("水平线波动", self.flat_threshold_input), 0, 2)
        brake_grid.addWidget(self._inline_form_field("保持时间", self.hold_ms_input), 1, 0)
        self.backtrack_field = self._inline_form_field("回溯脉冲数", self.backtrack_pulses_input)
        brake_grid.addWidget(self.backtrack_field, 1, 1)
        layout.addLayout(brake_grid)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.run_button = QPushButton("执行测试")
        self.apply_startup_cursor_button = QPushButton("定位启动游标")
        self.apply_brake_cursor_button = QPushButton("定位刹车游标")
        self.export_stats_button = QPushButton("导出统计 CSV")
        self.clear_stats_button = QPushButton("清空统计")
        self.apply_startup_cursor_button.setEnabled(False)
        self.apply_brake_cursor_button.setEnabled(False)
        button_row.addWidget(self.run_button)
        button_row.addSpacing(12)
        button_row.addWidget(self.apply_startup_cursor_button)
        button_row.addWidget(self.apply_brake_cursor_button)
        button_row.addSpacing(12)
        button_row.addWidget(self.export_stats_button)
        button_row.addWidget(self.clear_stats_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        result_stats_row = QHBoxLayout()
        result_stats_row.setSpacing(12)

        single_result_box = self._group_box("单次结果")
        single_result_layout = QVBoxLayout(single_result_box)
        single_result_layout.setContentsMargins(10, 10, 10, 10)
        single_result_layout.setSpacing(8)
        self.result_labels: dict[str, QLabel] = {}
        self.result_cards: dict[str, QWidget] = {}
        result_items = [
            ("启动起点", "startup_start"),
            ("达速时刻", "startup_reach"),
            ("启动时长", "startup_delay"),
            ("启动峰值电流", "startup_peak"),
            ("峰值时刻", "startup_peak_time"),
            ("刹车起点", "brake_start"),
            ("电流归零确认", "current_zero"),
            ("刹车终点", "brake_end"),
            ("刹车时长", "brake_delay"),
            ("刹车峰值电流", "brake_peak"),
            ("命中频率", "speed_frequency"),
            ("命中周期", "speed_period"),
        ]
        results_grid = QGridLayout()
        results_grid.setHorizontalSpacing(10)
        results_grid.setVerticalSpacing(10)
        for index, (title, key) in enumerate(result_items):
            value_label = self._metric_value_label()
            self.result_labels[key] = value_label
            card = self._metric_card(title, value_label)
            self.result_cards[key] = card
            results_grid.addWidget(card, index // 4, index % 4)
        single_result_layout.addLayout(results_grid)
        result_stats_row.addWidget(single_result_box, 2)

        stats_box = self._group_box("统计范围")
        stats_box_layout = QVBoxLayout(stats_box)
        stats_box_layout.setContentsMargins(10, 10, 10, 10)
        stats_box_layout.setSpacing(8)
        self.stats_labels: dict[str, QLabel] = {}
        stats_items = [
            ("样本数", "sample_count"),
            ("启动时长范围", "startup_delay_range"),
            ("刹车时长范围", "brake_delay_range"),
            ("启动峰值电流范围", "startup_peak_range"),
            ("刹车峰值电流范围", "brake_peak_range"),
            ("命中频率范围", "speed_frequency_range"),
        ]
        stats_grid = QGridLayout()
        stats_grid.setHorizontalSpacing(10)
        stats_grid.setVerticalSpacing(10)
        stats_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        for index, (title, key) in enumerate(stats_items):
            value_label = self._metric_value_label()
            self.stats_labels[key] = value_label
            stats_grid.addWidget(self._metric_card(title, value_label), index // 2, index % 2)
        stats_box_layout.addLayout(stats_grid)
        result_stats_row.addWidget(stats_box, 1)
        layout.addLayout(result_stats_row)

        history_title = QLabel("测试记录")
        history_title.setFont(QFont(history_title.font().family(), history_title.font().pointSize(), QFont.Bold))
        layout.addWidget(history_title)

        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ["#", "时间", "启动(ms)", "刹车(ms)", "启动峰值(A)", "刹车峰值(A)", "命中频率(Hz)"]
        )
        self.history_table.setMinimumHeight(220)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionMode(QTableWidget.NoSelection)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(False)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.history_table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.history_table.verticalHeader().setDefaultSectionSize(28)
        layout.addWidget(self.history_table, 1)

        self.summary_label = QLabel(self.DEFAULT_SUMMARY_TEXT)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return box

    def _inline_form_field(self, text: str, widget: QWidget) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._form_label(text))
        row.addWidget(widget)
        row.addStretch(1)
        return container

    def _metric_card(self, title: str, value_label: QLabel) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(title_label)
        card_layout.addWidget(value_label)
        return card

    def _metric_value_label(self) -> QLabel:
        label = QLabel("-")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        value_font = label.font()
        value_font.setBold(True)
        label.setFont(value_font)
        return label

    def _form_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setFixedWidth(84)
        return label

    def _set_compact_field_width(self, *widgets: QWidget) -> None:
        for widget in widgets:
            widget.setMinimumWidth(128)
            widget.setMaximumWidth(156)

    def _create_channel_combo(self, default_channel: str) -> QComboBox:
        combo = QComboBox()
        for channel in SUPPORTED_CHANNELS:
            combo.addItem(_display_channel_name(channel), channel)
        index = combo.findData(default_channel)
        if index >= 0:
            combo.setCurrentIndex(index)
        return combo

    def _selected_channel_from_combo(self, combo: QComboBox) -> str:
        current = combo.currentData()
        if isinstance(current, str) and current:
            return current
        return _normalize_channel_name(combo.currentText())

    def _refresh_channel_options(self, changed_combo: QComboBox | None = None) -> None:
        if changed_combo is None:
            self.channel_previous = {
                id(combo): self._selected_channel_from_combo(combo) for combo in self.channel_combos
            }
            return

        current_channel = self._selected_channel_from_combo(changed_combo)
        previous_channel = self.channel_previous.get(id(changed_combo), current_channel)
        conflict_combo = next(
            (
                combo
                for combo in self.channel_combos
                if combo is not changed_combo and self._selected_channel_from_combo(combo) == current_channel
            ),
            None,
        )
        if conflict_combo is not None and previous_channel != current_channel:
            target_index = conflict_combo.findData(previous_channel)
            if target_index >= 0:
                conflict_combo.blockSignals(True)
                conflict_combo.setCurrentIndex(target_index)
                conflict_combo.blockSignals(False)

        self.channel_previous = {
            id(combo): self._selected_channel_from_combo(combo) for combo in self.channel_combos
        }

    def _refresh_mode_fields(self) -> None:
        brake_mode = str(self.brake_mode_combo.currentData())
        encoder_enabled = brake_mode == "encoder_backtrack"
        self.encoder_field.setEnabled(encoder_enabled)
        self.backtrack_field.setEnabled(encoder_enabled)
        self._refresh_result_emphasis(brake_mode)

    def _refresh_target_fields(self) -> None:
        target_mode = str(self.target_mode_combo.currentData())
        target_value = float(self.target_value_input.value())
        pulses_per_revolution = max(int(self.ppr_input.value()), 1)

        self.ppr_field.setEnabled(target_mode == "rpm")

        if target_mode == "rpm":
            frequency_hz = (target_value * pulses_per_revolution) / 60.0
            period_ms = (1000.0 / frequency_hz) if frequency_hz > 0 else 0.0
            self.target_hint_label.setText(
                f"当前按转速判定：{target_value:.3f} RPM -> {frequency_hz:.6f} Hz -> {period_ms:.6f} ms"
            )
        elif target_mode == "frequency_hz":
            period_ms = (1000.0 / target_value) if target_value > 0 else 0.0
            self.target_hint_label.setText(
                f"当前按频率判定：{target_value:.6f} Hz -> {period_ms:.6f} ms"
            )
        elif target_mode == "period_ms":
            frequency_hz = (1000.0 / target_value) if target_value > 0 else 0.0
            self.target_hint_label.setText(
                f"当前按周期判定：{target_value:.6f} ms -> {frequency_hz:.6f} Hz"
            )
        else:
            self.target_hint_label.setText("")

    def _centered_table_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(int(Qt.AlignCenter))
        return item

    def _refresh_result_emphasis(self, brake_mode: str | None = None) -> None:
        if brake_mode is None:
            brake_mode = str(self.brake_mode_combo.currentData())

        if brake_mode == "current_zero":
            muted_keys = {"brake_end"}
        elif brake_mode == "encoder_backtrack":
            muted_keys = {"current_zero"}
        else:
            muted_keys = set()

        for key, card in self.result_cards.items():
            card.setEnabled(key not in muted_keys)

    def _stabilize_push_buttons(self, container: QWidget) -> None:
        for button in container.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setMinimumHeight(max(button.minimumHeight(), 30))

    def _normalize_label_alignment(self, container: QWidget) -> None:
        for label in container.findChildren(QLabel):
            label.setAlignment(label.alignment() | Qt.AlignVCenter)

    def _config_from_ui(self) -> StartupBrakeTestConfig:
        return StartupBrakeTestConfig(
            control_channel=self._selected_channel_from_combo(self.control_channel_combo),
            speed_channel=self._selected_channel_from_combo(self.speed_channel_combo),
            current_channel=self._selected_channel_from_combo(self.current_channel_combo),
            encoder_a_channel=self._selected_channel_from_combo(self.encoder_channel_combo),
            speed_target_mode=str(self.target_mode_combo.currentData()),
            speed_target_value=float(self.target_value_input.value()),
            speed_tolerance_ratio=float(self.tolerance_input.value()) / 100.0,
            speed_consecutive_periods=int(self.consecutive_input.value()),
            pulses_per_revolution=int(self.ppr_input.value()),
            control_threshold_ratio=0.1,
            zero_current_threshold_a=float(self.zero_threshold_input.value()),
            zero_current_flat_threshold_a=float(self.flat_threshold_input.value()),
            zero_current_hold_s=float(self.hold_ms_input.value()) / 1000.0,
            brake_mode=str(self.brake_mode_combo.currentData()),
            brake_backtrack_pulses=int(self.backtrack_pulses_input.value()),
        )

    def _required_channels(self, config: StartupBrakeTestConfig) -> list[str]:
        channels: list[str] = []
        for channel in (
            config.control_channel,
            config.speed_channel,
            config.current_channel,
            config.encoder_a_channel if config.brake_mode == "encoder_backtrack" else None,
        ):
            if channel and channel not in channels:
                channels.append(channel)
        return channels

    def run_test(self) -> None:
        config = self._config_from_ui()
        required_channels = self._required_channels(config)
        available_channels = {waveform.channel for waveform in self.main_window.last_waveform_bundle}
        if required_channels and set(required_channels).issubset(available_channels):
            self._execute_test(self.main_window.last_waveform_bundle, config)
            return

        scope = self.main_window.scope
        if scope is None or not scope.is_connected:
            self.main_window._show_warning("当前波形缺少测试所需通道，请先连接示波器或加载包含这些通道的波形文件。")
            return

        points_mode = self.main_window.waveform_mode_combo.currentText()
        points = int(self.main_window.waveform_points_input.value())
        self.summary_label.setText("正在抓取启动刹车测试所需波形...")
        self.main_window.log(
            "启动刹车测试补抓波形: "
            + ", ".join(_display_channel_name(channel) for channel in required_channels)
        )
        self.main_window._run_task(
            lambda: [scope.fetch_waveform(channel, points_mode=points_mode, points=points) for channel in required_channels],
            on_success=lambda waveforms, captured_config=config: self._on_waveforms_ready(waveforms, captured_config),
            success_message="启动刹车测试波形抓取完成。",
        )

    def _on_waveforms_ready(
        self,
        waveforms: list[WaveformData],
        config: StartupBrakeTestConfig,
    ) -> None:
        self.main_window._on_waveforms_fetched(waveforms)
        self._execute_test(waveforms, config)

    def _execute_test(
        self,
        waveforms: list[WaveformData],
        config: StartupBrakeTestConfig,
    ) -> None:
        try:
            result = analyze_startup_brake_test(waveforms, config)
        except Exception as exc:
            self.last_result = None
            self.clear_results(reset_summary=False)
            self.summary_label.setText(f"测试失败：{exc}")
            self.main_window.log(f"启动刹车性能测试失败: {exc}")
            self.main_window._show_warning(str(exc))
            return

        self.last_result = result
        self.history.append(
            StartupBrakeHistoryEntry(
                result=result,
                timestamp=datetime.now().strftime("%H:%M:%S"),
                config=config,
            )
        )
        self._update_results(result)
        self._refresh_history()
        self.main_window.log(
            "启动刹车性能测试完成: "
            f"启动 {result.startup_delay_s:.6e}s, 刹车 {result.brake_delay_s:.6e}s, "
            f"命中频率 {result.speed_match.frequency_hz:.3f}Hz"
        )

    def _update_results(self, result: StartupBrakeTestResult) -> None:
        self.result_labels["startup_start"].setText(f"{result.startup_start_point[0]:.6e} s")
        self.result_labels["startup_reach"].setText(f"{result.speed_reached_point[0]:.6e} s")
        self.result_labels["startup_delay"].setText(f"{result.startup_delay_s:.6e} s")
        self.result_labels["startup_peak"].setText(_format_peak_current(result.startup_peak_current))
        self.result_labels["startup_peak_time"].setText(_format_peak_time(result.startup_peak_current))
        self.result_labels["brake_start"].setText(f"{result.brake_start_point[0]:.6e} s")
        self.result_labels["current_zero"].setText(f"{result.current_zero_window.confirmed_time_s:.6e} s")
        self.result_labels["brake_end"].setText(f"{result.brake_end_point[0]:.6e} s")
        self.result_labels["brake_delay"].setText(f"{result.brake_delay_s:.6e} s")
        self.result_labels["brake_peak"].setText(_format_peak_current(result.brake_peak_current))
        self.result_labels["speed_frequency"].setText(f"{result.speed_match.frequency_hz:.6f} Hz")
        self.result_labels["speed_period"].setText(f"{result.speed_match.period_s * 1000.0:.6f} ms")
        self.apply_startup_cursor_button.setEnabled(True)
        self.apply_brake_cursor_button.setEnabled(True)
        brake_mode_label = "电流归零" if result.brake_mode == "current_zero" else "A相回溯"
        self.summary_label.setText(
            "启动刹车性能测试完成："
            f"第 {len(self.history)} 次样本，"
            f"启动 {result.startup_delay_s:.6e}s，"
            f"刹车 {result.brake_delay_s:.6e}s，"
            f"模式 {brake_mode_label}。"
        )

    def clear_results(self, *, reset_summary: bool = True) -> None:
        for label in self.result_labels.values():
            label.setText("-")
        self._refresh_result_emphasis()
        self.apply_startup_cursor_button.setEnabled(False)
        self.apply_brake_cursor_button.setEnabled(False)
        if reset_summary:
            self.summary_label.setText(self.DEFAULT_SUMMARY_TEXT)

    def clear_history(self) -> None:
        self.history = []
        self._refresh_history()
        self.summary_label.setText("统计已清空。可继续执行测试重新累计范围。")

    def _export_history_csv(self) -> None:
        if not self.history:
            self.main_window._show_warning("当前没有可导出的启动刹车测试统计。")
            return

        STARTUP_BRAKE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = STARTUP_BRAKE_DIR / f"startup_brake_stats_{timestamp}.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出启动刹车统计 CSV",
            str(default_path),
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        output_path = Path(file_path)
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["section", "key", "value"])
            writer.writerow(["config", "control_channel", self._history_config_summary(lambda config: _display_channel_name(config.control_channel))])
            writer.writerow(["config", "speed_channel", self._history_config_summary(lambda config: _display_channel_name(config.speed_channel))])
            writer.writerow(["config", "current_channel", self._history_config_summary(lambda config: _display_channel_name(config.current_channel))])
            writer.writerow(["config", "encoder_a_channel", self._history_config_summary(lambda config: _display_channel_name(config.encoder_a_channel or "-"))])
            writer.writerow(["config", "speed_target_mode", self._history_config_summary(lambda config: self._target_mode_display_text(config.speed_target_mode))])
            writer.writerow(["config", "speed_target_value", self._history_config_summary(lambda config: f"{config.speed_target_value:.6f}")])
            writer.writerow(["config", "speed_tolerance_percent", self._history_config_summary(lambda config: f"{config.speed_tolerance_ratio * 100.0:.2f}")])
            writer.writerow(["config", "speed_consecutive_periods", self._history_config_summary(lambda config: str(config.speed_consecutive_periods))])
            writer.writerow(["config", "pulses_per_revolution", self._history_config_summary(lambda config: str(config.pulses_per_revolution))])
            writer.writerow(["config", "brake_mode", self._history_config_summary(lambda config: self._brake_mode_display_text(config.brake_mode))])
            writer.writerow(["config", "zero_current_threshold_a", self._history_config_summary(lambda config: f"{config.zero_current_threshold_a:.6f}")])
            writer.writerow(["config", "zero_current_flat_threshold_a", self._history_config_summary(lambda config: f"{config.zero_current_flat_threshold_a:.6f}")])
            writer.writerow(["config", "zero_current_hold_ms", self._history_config_summary(lambda config: f"{config.zero_current_hold_s * 1000.0:.6f}")])
            writer.writerow(["config", "brake_backtrack_pulses", self._history_config_summary(lambda config: str(config.brake_backtrack_pulses))])
            writer.writerow([])

            writer.writerow(["summary", "sample_count", str(len(self.history))])
            writer.writerow(
                ["summary", "startup_delay_range_ms", _format_range_ms([entry.result.startup_delay_s * 1000.0 for entry in self.history])]
            )
            writer.writerow(
                ["summary", "brake_delay_range_ms", _format_range_ms([entry.result.brake_delay_s * 1000.0 for entry in self.history])]
            )
            writer.writerow(
                [
                    "summary",
                    "startup_peak_range_a",
                    _format_range_amp(
                        [entry.result.startup_peak_current.value for entry in self.history if entry.result.startup_peak_current is not None]
                    ),
                ]
            )
            writer.writerow(
                [
                    "summary",
                    "brake_peak_range_a",
                    _format_range_amp(
                        [entry.result.brake_peak_current.value for entry in self.history if entry.result.brake_peak_current is not None]
                    ),
                ]
            )
            writer.writerow(
                ["summary", "speed_frequency_range_hz", _format_range_hz([entry.result.speed_match.frequency_hz for entry in self.history])]
            )
            writer.writerow([])

            writer.writerow(
                [
                    "sample_index",
                    "timestamp",
                    "startup_delay_ms",
                    "brake_delay_ms",
                    "startup_peak_current_a",
                    "brake_peak_current_a",
                    "speed_frequency_hz",
                    "speed_period_ms",
                    "target_mode",
                    "target_value",
                    "pulses_per_revolution",
                    "brake_mode",
                ]
            )
            for index, entry in enumerate(self.history, start=1):
                result = entry.result
                config = entry.config
                writer.writerow(
                    [
                        index,
                        entry.timestamp,
                        f"{result.startup_delay_s * 1000.0:.6f}",
                        f"{result.brake_delay_s * 1000.0:.6f}",
                        f"{result.startup_peak_current.value:.6f}" if result.startup_peak_current is not None else "",
                        f"{result.brake_peak_current.value:.6f}" if result.brake_peak_current is not None else "",
                        f"{result.speed_match.frequency_hz:.6f}",
                        f"{result.speed_match.period_s * 1000.0:.6f}",
                        self._target_mode_display_text(config.speed_target_mode) if config is not None else "",
                        f"{config.speed_target_value:.6f}" if config is not None else "",
                        str(config.pulses_per_revolution) if config is not None else "",
                        self._brake_mode_display_text(result.brake_mode),
                    ]
                )

        self.summary_label.setText(f"统计 CSV 已导出：{output_path}")
        self.main_window.log(f"启动刹车统计已导出: {output_path}")

    def _apply_startup_cursors(self) -> None:
        if self.last_result is None:
            self.main_window._show_warning("请先执行一次启动刹车性能测试。")
            return
        self.main_window.waveform_panel.set_cursor_points(
            self.last_result.startup_start_point,
            self.last_result.speed_reached_point,
            annotation_text="Startup Window",
        )
        if self.main_window.waveform_detail_dialog.isVisible():
            self.main_window.waveform_detail_dialog.set_cursor_points(
                self.last_result.startup_start_point,
                self.last_result.speed_reached_point,
                annotation_text="Startup Window",
            )

    def _apply_brake_cursors(self) -> None:
        if self.last_result is None:
            self.main_window._show_warning("请先执行一次启动刹车性能测试。")
            return
        self.main_window.waveform_panel.set_cursor_points(
            self.last_result.brake_start_point,
            self.last_result.brake_end_point,
            annotation_text="Brake Window",
        )
        if self.main_window.waveform_detail_dialog.isVisible():
            self.main_window.waveform_detail_dialog.set_cursor_points(
                self.last_result.brake_start_point,
                self.last_result.brake_end_point,
                annotation_text="Brake Window",
            )

    def _refresh_history(self) -> None:
        self.history_table.setRowCount(len(self.history))
        for row, entry in enumerate(self.history):
            result = entry.result
            self.history_table.setItem(row, 0, self._centered_table_item(str(row + 1)))
            self.history_table.setItem(row, 1, self._centered_table_item(entry.timestamp))
            self.history_table.setItem(row, 2, self._centered_table_item(f"{result.startup_delay_s * 1000.0:.3f} ms"))
            self.history_table.setItem(row, 3, self._centered_table_item(f"{result.brake_delay_s * 1000.0:.3f} ms"))
            self.history_table.setItem(row, 4, self._centered_table_item(_format_peak_current(result.startup_peak_current)))
            self.history_table.setItem(row, 5, self._centered_table_item(_format_peak_current(result.brake_peak_current)))
            self.history_table.setItem(row, 6, self._centered_table_item(f"{result.speed_match.frequency_hz:.6f} Hz"))

        if not self.history:
            for label in self.stats_labels.values():
                label.setText("-")
            return

        startup_delays_ms = [entry.result.startup_delay_s * 1000.0 for entry in self.history]
        brake_delays_ms = [entry.result.brake_delay_s * 1000.0 for entry in self.history]
        startup_peaks = [
            entry.result.startup_peak_current.value for entry in self.history if entry.result.startup_peak_current is not None
        ]
        brake_peaks = [
            entry.result.brake_peak_current.value for entry in self.history if entry.result.brake_peak_current is not None
        ]
        speed_frequencies = [entry.result.speed_match.frequency_hz for entry in self.history]

        self.stats_labels["sample_count"].setText(str(len(self.history)))
        self.stats_labels["startup_delay_range"].setText(_format_range_ms(startup_delays_ms))
        self.stats_labels["brake_delay_range"].setText(_format_range_ms(brake_delays_ms))
        self.stats_labels["startup_peak_range"].setText(_format_range_amp(startup_peaks))
        self.stats_labels["brake_peak_range"].setText(_format_range_amp(brake_peaks))
        self.stats_labels["speed_frequency_range"].setText(_format_range_hz(speed_frequencies))

    def _history_config_summary(self, getter) -> str:
        if not self.history:
            return "-"
        values = [getter(entry.config) for entry in self.history]
        first_value = values[0]
        if all(value == first_value for value in values[1:]):
            return first_value
        return "mixed"

    def _target_mode_display_text(self, target_mode: str) -> str:
        if target_mode == "frequency_hz":
            return "频率(Hz)"
        if target_mode == "period_ms":
            return "周期(ms)"
        if target_mode == "rpm":
            return "转速(RPM)"
        return target_mode

    def _brake_mode_display_text(self, brake_mode: str) -> str:
        if brake_mode == "current_zero":
            return "电流归零"
        if brake_mode == "encoder_backtrack":
            return "A相回溯"
        return brake_mode
