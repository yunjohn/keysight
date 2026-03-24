from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.device.instrument import (
    EdgeTriggerSettings,
    MEASUREMENT_DEFINITIONS,
    SUPPORTED_CHANNELS,
    SUPPORTED_TRIGGER_SLOPES,
    SUPPORTED_TRIGGER_SWEEPS,
    SUPPORTED_WAVEFORM_POINTS_MODES,
    ChannelVerticalLayout,
    KeysightOscilloscope,
    list_visa_resources,
)
from keysight_scope_app.infra.task_runner import BackgroundTaskRunner, RepeatingTaskHandle
from keysight_scope_app.analysis.waveform import WaveformData, WaveformStats
from keysight_scope_app.ui.dialogs.startup_brake import StartupBrakeTestDialog
from keysight_scope_app.ui.dialogs.waveform import WaveformDetailDialog
from keysight_scope_app.ui.helpers import display_channel_name, normalize_channel_name
from keysight_scope_app.ui.panels.waveform import WaveformAnalysisPanel


CAPTURE_DIR = Path("captures")
WAVEFORM_DIR = Path("captures") / "waveforms"
UI_STATE_PATH = CAPTURE_DIR / "ui_state.json"
MAX_LOG_LINES = 300
DEFAULT_MEASUREMENT_SET = {"频率", "峰峰值", "均方根"}
MEASUREMENT_TEMPLATES = {
    "基础模板": {"频率", "周期", "峰峰值", "均方根"},
    "方波模板": {"频率", "周期", "峰峰值", "占空比", "正脉宽", "负脉宽", "上升时间", "下降时间"},
    "纹波模板": {"峰峰值", "均方根", "最大值", "最小值"},
    "边沿模板": {"最大值", "最小值", "高电平估计", "低电平估计", "上升时间", "下降时间"},
}
WAVEFORM_MODE_HINTS = {
    "NORMal": "NORMal：常规模式，抓取速度和点数比较均衡，适合日常查看波形。",
    "MAXimum": "MAXimum：尽量返回更多显示细节，适合比 NORMal 更关注局部波形时使用。",
    "RAW": "RAW：尽量读取更接近原始采样内存的数据，点数更多，适合启动刹车、边沿和局部放大分析。",
}
WAVEFORM_MODE_DEFAULT_POINTS = {
    "NORMal": 2000,
    "MAXimum": 10000,
    "RAW": 20000,
}

class ScopeMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.scope: KeysightOscilloscope | None = None
        self.auto_measure_handle: RepeatingTaskHandle | None = None
        self.task_runner = BackgroundTaskRunner()
        self.log_lines: list[str] = []
        self.measurement_checks: dict[str, QCheckBox] = {}
        self.scope_display_checks: dict[str, QCheckBox] = {}
        self.detected_channel_units: dict[str, str] = {channel: "V" for channel in SUPPORTED_CHANNELS}
        self.channel_unit_overrides: dict[str, str | None] = {channel: None for channel in SUPPORTED_CHANNELS}
        self.channel_unit_combos: dict[str, QComboBox] = {}
        self.channel_vertical_layouts: dict[str, dict[str, float]] = {}
        self._connect_request_id = 0
        self._updating_scope_display_checks = False
        self._persist_ui_settings_enabled = False
        self._waveform_mode_max_points_hint = ""
        self.last_capture_path: Path | None = None
        self.last_waveform_bundle: list[WaveformData] = []
        self.last_waveform_data: WaveformData | None = None
        self.last_waveform_stats: WaveformStats | None = None
        self.waveform_detail_dialog = WaveformDetailDialog(self)

        self.setWindowTitle("Keysight 示波器助手")
        self.resize(1480, 920)
        self._build_ui()
        self._build_timer()
        self.log("界面已启动。请先点击“刷新资源”，确认示波器地址后再连接。")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        left_panel = QVBoxLayout()
        left_panel.setSpacing(12)
        right_panel = QVBoxLayout()
        right_panel.setSpacing(12)
        root.addLayout(left_panel, 11)
        root.addLayout(right_panel, 14)

        top_status = QGridLayout()
        top_status.setHorizontalSpacing(12)
        left_panel.addLayout(top_status)

        self.status_value = QLabel("未连接")
        self.idn_value = QLabel("-")
        self.capture_value = QLabel("-")
        top_status.addWidget(self._build_status_card("连接状态", self.status_value), 0, 0)
        top_status.addWidget(self._build_status_card("设备标识", self.idn_value), 0, 1)
        top_status.addWidget(self._build_status_card("最近截图", self.capture_value), 0, 2)

        connection_box = self._group_box("设备连接")
        connection_layout = QGridLayout(connection_box)
        connection_layout.setHorizontalSpacing(10)
        connection_layout.setVerticalSpacing(8)

        self.resource_combo = QComboBox()
        self.resource_combo.setEditable(True)
        self.resource_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.resource_combo.setInsertPolicy(QComboBox.NoInsert)
        self.resource_combo.lineEdit().setPlaceholderText("例如 USB0::0x2A8D::0x1766::MYxxxx::0::INSTR")

        self.refresh_button = QPushButton("刷新资源")
        self.connect_button = QPushButton("连接设备")
        self.disconnect_button = QPushButton("断开连接")
        self.error_button = QPushButton("读取错误")

        connection_layout.addWidget(QLabel("资源地址"), 0, 0)
        connection_layout.addWidget(self.resource_combo, 0, 1)
        connection_layout.addWidget(self.refresh_button, 0, 2)
        connection_layout.addWidget(self.connect_button, 0, 3)
        connection_layout.addWidget(self.disconnect_button, 0, 4)
        connection_layout.addWidget(self.error_button, 0, 5)

        self.resource_hint = QLabel("提示：优先选择带真实序列号的资源地址。")
        self.resource_hint.setWordWrap(True)
        connection_layout.addWidget(self.resource_hint, 1, 0, 1, 6)
        left_panel.addWidget(connection_box)

        trigger_box = self._group_box("触发设置")
        trigger_layout = QGridLayout(trigger_box)
        trigger_layout.setHorizontalSpacing(10)
        trigger_layout.setVerticalSpacing(8)
        self.trigger_source_combo = QComboBox()
        for channel in SUPPORTED_CHANNELS:
            self.trigger_source_combo.addItem(display_channel_name(channel), channel)
        self.trigger_slope_combo = QComboBox()
        slope_labels = {
            "POSitive": "上升沿",
            "NEGative": "下降沿",
            "EITHer": "双边沿",
        }
        for slope in SUPPORTED_TRIGGER_SLOPES:
            self.trigger_slope_combo.addItem(slope_labels[slope], slope)
        self.trigger_level_input = QDoubleSpinBox()
        self.trigger_level_input.setRange(-1_000_000.0, 1_000_000.0)
        self.trigger_level_input.setDecimals(6)
        self.trigger_level_input.setSingleStep(0.1)
        self.trigger_sweep_combo = QComboBox()
        sweep_labels = {
            "AUTO": "AUTO",
            "NORMal": "NORM",
        }
        for sweep in SUPPORTED_TRIGGER_SWEEPS:
            self.trigger_sweep_combo.addItem(sweep_labels[sweep], sweep)
        self.read_trigger_button = QPushButton("读取触发")
        self.apply_trigger_button = QPushButton("应用触发")
        self.read_trigger_status_button = QPushButton("读取状态")
        self.single_trigger_button = QPushButton("单次等待触发")
        self.trigger_status_value = QLabel("边沿触发：未读取")
        self.trigger_status_value.setWordWrap(True)
        self.trigger_event_value = QLabel("触发状态：未读取")
        self.trigger_event_value.setWordWrap(True)

        trigger_layout.addWidget(QLabel("触发源"), 0, 0)
        trigger_layout.addWidget(self.trigger_source_combo, 0, 1)
        trigger_layout.addWidget(QLabel("边沿"), 0, 2)
        trigger_layout.addWidget(self.trigger_slope_combo, 0, 3)
        trigger_layout.addWidget(QLabel("电平"), 0, 4)
        trigger_layout.addWidget(self.trigger_level_input, 0, 5)
        trigger_layout.addWidget(QLabel("模式"), 0, 6)
        trigger_layout.addWidget(self.trigger_sweep_combo, 0, 7)
        trigger_layout.addWidget(self.read_trigger_button, 1, 4)
        trigger_layout.addWidget(self.apply_trigger_button, 1, 5)
        trigger_layout.addWidget(self.read_trigger_status_button, 1, 6)
        trigger_layout.addWidget(self.single_trigger_button, 1, 7)
        trigger_layout.addWidget(self.trigger_status_value, 1, 0, 1, 6)
        trigger_layout.addWidget(self.trigger_event_value, 2, 0, 1, 8)
        left_panel.addWidget(trigger_box)

        measure_box = self._group_box("采集与测量")
        measure_layout = QVBoxLayout(measure_box)
        top_row = QHBoxLayout()

        self.channel_combo = QComboBox()
        for channel in SUPPORTED_CHANNELS:
            self.channel_combo.addItem(display_channel_name(channel), channel)
        self.interval_input = QDoubleSpinBox()
        self.interval_input.setRange(0.2, 10.0)
        self.interval_input.setSingleStep(0.2)
        self.interval_input.setValue(1.0)
        self.measurement_status = QLabel("自动测量：未启动")
        self.measurement_status.setFont(QFont(self.measurement_status.font().family(), self.measurement_status.font().pointSize(), QFont.Bold))
        self.last_update_value = QLabel("最近更新：-")

        top_row.addWidget(QLabel("测量通道"))
        top_row.addWidget(self.channel_combo)
        top_row.addSpacing(16)
        top_row.addWidget(QLabel("轮询间隔(s)"))
        top_row.addWidget(self.interval_input)
        top_row.addSpacing(16)
        top_row.addWidget(self.measurement_status)
        top_row.addStretch(1)
        top_row.addWidget(self.last_update_value)
        measure_layout.addLayout(top_row)

        selection_row = QHBoxLayout()
        self.select_default_button = QPushButton("默认项")
        self.select_all_button = QPushButton("全选")
        self.clear_selection_button = QPushButton("清空")
        self.single_acquire_button = QPushButton("SINGLE")
        self.measurement_count_label = QLabel()
        selection_row.addWidget(QLabel("测量项"))
        selection_row.addWidget(self.select_default_button)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.clear_selection_button)
        selection_row.addSpacing(16)
        selection_row.addWidget(self.single_acquire_button)
        selection_row.addStretch(1)
        selection_row.addWidget(self.measurement_count_label)
        measure_layout.addLayout(selection_row)

        checks_layout = QGridLayout()
        checks_layout.setHorizontalSpacing(18)
        checks_layout.setVerticalSpacing(8)
        for index, name in enumerate(MEASUREMENT_DEFINITIONS):
            checkbox = QCheckBox(name)
            checkbox.setChecked(name in DEFAULT_MEASUREMENT_SET)
            self.measurement_checks[name] = checkbox
            checkbox.toggled.connect(self._update_measurement_count)
            checks_layout.addWidget(checkbox, index // 3, index % 3)
        measure_layout.addLayout(checks_layout)

        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("测量模板"))
        for template_name in MEASUREMENT_TEMPLATES:
            button = QPushButton(template_name)
            button.clicked.connect(lambda checked=False, name=template_name: self._apply_measurement_template(name))
            template_row.addWidget(button)
        template_row.addStretch(1)
        measure_layout.addLayout(template_row)

        action_row = QHBoxLayout()
        self.single_button = QPushButton("单次测量")
        self.auto_measure_button = QPushButton("启动自动测量")
        self.auto_measure_button.setMinimumWidth(132)
        self.autoscale_button = QPushButton("AUToscale")
        self.run_button = QPushButton("RUN")
        self.stop_button = QPushButton("STOP")
        for button in (
            self.single_button,
            self.auto_measure_button,
            self.autoscale_button,
            self.run_button,
            self.stop_button,
        ):
            action_row.addWidget(button)
        measure_layout.addLayout(action_row)

        waveform_row = QHBoxLayout()
        self.waveform_mode_combo = QComboBox()
        self.waveform_mode_combo.addItems(SUPPORTED_WAVEFORM_POINTS_MODES)
        self.waveform_mode_combo.currentTextChanged.connect(self._on_waveform_mode_changed)
        self.waveform_points_input = QDoubleSpinBox()
        self.waveform_points_input.setDecimals(0)
        self.waveform_points_input.setRange(100, 500000)
        self.waveform_points_input.setSingleStep(100)
        self.waveform_points_input.setValue(2000)
        self.fetch_waveform_button = QPushButton("抓取波形")
        self.load_waveform_button = QPushButton("加载 CSV")
        self.export_waveform_button = QPushButton("导出 CSV")
        self.export_waveform_button.setEnabled(False)
        waveform_row.addWidget(QLabel("波形模式"))
        waveform_row.addWidget(self.waveform_mode_combo)
        waveform_row.addSpacing(16)
        waveform_row.addWidget(QLabel("点数"))
        waveform_row.addWidget(self.waveform_points_input)
        waveform_row.addSpacing(16)
        waveform_row.addWidget(self.fetch_waveform_button)
        waveform_row.addWidget(self.load_waveform_button)
        waveform_row.addWidget(self.export_waveform_button)
        waveform_row.addStretch(1)
        measure_layout.addLayout(waveform_row)

        self.waveform_mode_hint_label = QLabel("")
        self.waveform_mode_hint_label.setWordWrap(True)
        measure_layout.addWidget(self.waveform_mode_hint_label)

        scope_display_row = QHBoxLayout()
        scope_display_row.addWidget(QLabel("示波器通道"))
        for channel in SUPPORTED_CHANNELS:
            checkbox = QCheckBox(display_channel_name(channel))
            checkbox.setEnabled(False)
            checkbox.toggled.connect(lambda checked=False, target_channel=channel: self._toggle_scope_channel_display(target_channel, checked))
            self.scope_display_checks[channel] = checkbox
            scope_display_row.addWidget(checkbox)
        scope_display_row.addStretch(1)
        measure_layout.addLayout(scope_display_row)

        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("通道单位"))
        for channel in SUPPORTED_CHANNELS:
            unit_row.addWidget(QLabel(display_channel_name(channel)))
            combo = QComboBox()
            combo.addItem("自动(V)", None)
            combo.addItem("电压(V)", "V")
            combo.addItem("电流(A)", "A")
            combo.currentIndexChanged.connect(
                lambda index=0, target_channel=channel: self._set_channel_unit_override(
                    target_channel,
                    self.channel_unit_combos[target_channel].currentData(),
                )
            )
            self.channel_unit_combos[channel] = combo
            unit_row.addWidget(combo)
        unit_row.addStretch(1)
        measure_layout.addLayout(unit_row)
        self._sync_channel_unit_controls()

        self.waveform_summary = QLabel("波形状态：尚未抓取")
        self.waveform_summary.setWordWrap(True)
        measure_layout.addWidget(self.waveform_summary)
        left_panel.addWidget(measure_box)

        result_box = self._group_box("测量结果")
        result_layout = QVBoxLayout(result_box)
        self.result_table = QTableWidget(0, 5)
        self.result_table.setHorizontalHeaderLabels(["测量项", "显示值", "单位", "原始值", "更新时间"])
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.result_table.setSelectionMode(QTableWidget.NoSelection)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        result_layout.addWidget(self.result_table)

        screenshot_box = self._group_box("截图")
        screenshot_layout = QVBoxLayout(screenshot_box)
        self.capture_button = QPushButton("一键截图")
        screenshot_layout.addWidget(self.capture_button)
        self.preview_label = QLabel("暂无截图预览")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFrameShape(QFrame.StyledPanel)
        self.preview_label.setMinimumSize(240, 180)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        screenshot_layout.addWidget(self.preview_label, 1)

        result_splitter = QSplitter(Qt.Horizontal)
        result_splitter.setChildrenCollapsible(False)
        result_splitter.addWidget(result_box)
        result_splitter.addWidget(screenshot_box)
        result_splitter.setStretchFactor(0, 1)
        result_splitter.setStretchFactor(1, 1)
        result_splitter.setSizes([520, 520])
        left_panel.addWidget(result_splitter, 1)

        log_box = self._group_box("运行日志")
        log_layout = QVBoxLayout(log_box)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        left_panel.addWidget(log_box, 1)

        waveform_box = self._group_box("波形分析")
        waveform_layout = QVBoxLayout(waveform_box)
        waveform_toolbar = QHBoxLayout()
        self.sync_waveform_button = QPushButton("独立波形显示")
        self.open_startup_brake_button = QPushButton("启动刹车测试")
        waveform_toolbar.addWidget(self.sync_waveform_button)
        waveform_toolbar.addWidget(self.open_startup_brake_button)
        waveform_toolbar.addStretch(1)
        waveform_layout.addLayout(waveform_toolbar)

        self.waveform_panel = WaveformAnalysisPanel(self, compact_mode=True)
        self.waveform_panel.channel_unit_resolver = self._channel_unit
        waveform_layout.addWidget(self.waveform_panel)
        right_panel.addWidget(waveform_box, 1)

        self.startup_brake_dialog = StartupBrakeTestDialog(self)

        self.refresh_button.clicked.connect(self.refresh_resources)
        self.connect_button.clicked.connect(self.connect_scope)
        self.disconnect_button.clicked.connect(self.disconnect_scope)
        self.error_button.clicked.connect(self.query_system_error)
        self.single_button.clicked.connect(self.run_single_measurement)
        self.auto_measure_button.clicked.connect(self.toggle_auto_measurement)
        self.single_acquire_button.clicked.connect(self.single_acquire)
        self.autoscale_button.clicked.connect(self.autoscale)
        self.run_button.clicked.connect(self.run_scope)
        self.stop_button.clicked.connect(self.stop_scope)
        self.capture_button.clicked.connect(self.capture_screenshot)
        self.read_trigger_button.clicked.connect(self.read_trigger_settings)
        self.apply_trigger_button.clicked.connect(self.apply_trigger_settings)
        self.read_trigger_status_button.clicked.connect(self.read_trigger_status)
        self.single_trigger_button.clicked.connect(self.arm_single_trigger)
        self.resource_combo.activated.connect(self._resource_selected)
        self.select_default_button.clicked.connect(self._select_default_measurements)
        self.select_all_button.clicked.connect(self._select_all_measurements)
        self.clear_selection_button.clicked.connect(self._clear_measurements)
        self.fetch_waveform_button.clicked.connect(self.fetch_waveform)
        self.load_waveform_button.clicked.connect(self.load_waveform_csv)
        self.export_waveform_button.clicked.connect(self.export_waveform_csv)
        self.sync_waveform_button.clicked.connect(self.sync_waveform_detail_dialog)
        self.open_startup_brake_button.clicked.connect(self.show_startup_brake_dialog)
        self.waveform_points_input.valueChanged.connect(lambda _: self._save_ui_state())
        self.trigger_source_combo.currentIndexChanged.connect(lambda _: self._save_ui_state())
        self.trigger_slope_combo.currentIndexChanged.connect(lambda _: self._save_ui_state())
        self.trigger_level_input.valueChanged.connect(lambda _: self._save_ui_state())
        self.trigger_sweep_combo.currentIndexChanged.connect(lambda _: self._save_ui_state())
        self._refresh_waveform_mode_hint(self.waveform_mode_combo.currentText())
        self._stabilize_push_buttons(self)
        self._normalize_label_alignment(self)
        self._load_ui_state()
        self._persist_ui_settings_enabled = True
        self._update_measurement_count()
        self._refresh_auto_measure_button()

    def _current_trigger_settings(self) -> EdgeTriggerSettings:
        return EdgeTriggerSettings(
            source=str(self.trigger_source_combo.currentData()),
            slope=str(self.trigger_slope_combo.currentData()),
            level=float(self.trigger_level_input.value()),
            sweep=str(self.trigger_sweep_combo.currentData()),
        )

    def _current_ui_state(self) -> dict:
        return {
            "waveform_mode": self.waveform_mode_combo.currentText(),
            "waveform_points": int(self.waveform_points_input.value()),
            "trigger": {
                "source": str(self.trigger_source_combo.currentData()),
                "slope": str(self.trigger_slope_combo.currentData()),
                "level": float(self.trigger_level_input.value()),
                "sweep": str(self.trigger_sweep_combo.currentData()),
            },
        }

    def _save_ui_state(self) -> None:
        if not self._persist_ui_settings_enabled:
            return
        UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with UI_STATE_PATH.open("w", encoding="utf-8") as state_file:
            json.dump(self._current_ui_state(), state_file, ensure_ascii=False, indent=2)

    def _load_ui_state(self) -> None:
        if not UI_STATE_PATH.exists():
            return
        try:
            payload = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"界面设置加载失败: {exc}")
            return

        waveform_mode = payload.get("waveform_mode")
        if waveform_mode in SUPPORTED_WAVEFORM_POINTS_MODES:
            self.waveform_mode_combo.setCurrentText(str(waveform_mode))
        waveform_points = payload.get("waveform_points")
        if isinstance(waveform_points, (int, float)):
            self.waveform_points_input.setValue(int(waveform_points))

        trigger_payload = payload.get("trigger")
        if isinstance(trigger_payload, dict):
            settings = EdgeTriggerSettings(
                source=str(trigger_payload.get("source", self.trigger_source_combo.currentData())),
                slope=str(trigger_payload.get("slope", self.trigger_slope_combo.currentData())),
                level=float(trigger_payload.get("level", self.trigger_level_input.value())),
                sweep=str(trigger_payload.get("sweep", self.trigger_sweep_combo.currentData())),
            )
            self._apply_trigger_settings_to_controls(settings)

    def _apply_trigger_settings_to_controls(self, settings: EdgeTriggerSettings) -> None:
        source_index = self.trigger_source_combo.findData(settings.source)
        slope_index = self.trigger_slope_combo.findData(settings.slope)
        sweep_index = self.trigger_sweep_combo.findData(settings.sweep)
        if source_index >= 0:
            self.trigger_source_combo.setCurrentIndex(source_index)
        if slope_index >= 0:
            self.trigger_slope_combo.setCurrentIndex(slope_index)
        if sweep_index >= 0:
            self.trigger_sweep_combo.setCurrentIndex(sweep_index)
        self.trigger_level_input.setValue(settings.level)
        self.trigger_status_value.setText(
            f"边沿触发：{display_channel_name(settings.source)} / "
            f"{self.trigger_slope_combo.currentText()} / {settings.level:.6f} / {self.trigger_sweep_combo.currentText()}"
        )
        self._save_ui_state()

    def _build_timer(self) -> None:
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.task_runner.drain_ui_queue)
        self.ui_timer.start(50)

    def _group_box(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        return box

    def _build_status_card(self, title: str, value_label: QLabel) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(card)
        title_label = QLabel(title)
        title_label.setFont(QFont(title_label.font().family(), title_label.font().pointSize(), QFont.Bold))
        value_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return card

    def _stabilize_push_buttons(self, container: QWidget) -> None:
        for button in container.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setMinimumHeight(max(button.minimumHeight(), 30))

    def _normalize_label_alignment(self, container: QWidget) -> None:
        for label in container.findChildren(QLabel):
            label.setAlignment(label.alignment() | Qt.AlignVCenter)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{timestamp}] {message}")
        self.log_lines = self.log_lines[-MAX_LOG_LINES:]
        self.log_text.setPlainText("\n".join(self.log_lines))
        self.log_text.moveCursor(QTextCursor.End)

    def refresh_resources(self) -> None:
        self.log("正在刷新 VISA 资源列表。")
        self._run_task(
            list_visa_resources,
            on_success=self._on_resources_loaded,
            success_message="资源列表刷新完成。",
        )

    def _on_resources_loaded(self, resources: tuple[str, ...]) -> None:
        current_text = self.resource_combo.currentText()
        self.resource_combo.blockSignals(True)
        self.resource_combo.clear()
        for resource in resources:
            self.resource_combo.addItem(resource)
        self.resource_combo.blockSignals(False)
        if resources:
            self.resource_combo.setCurrentText(resources[0])
            self.resource_hint.setText("提示：优先使用当前已选中的真实序列号地址。")
            self.log(f"发现 {len(resources)} 个资源，已优先选中可直接连接的地址。")
        else:
            self.resource_combo.setCurrentText(current_text)
            self.resource_hint.setText("未发现资源。请检查 Keysight IO Libraries Suite / NI-VISA 与 USB 连接。")
            self.log("未发现任何 VISA 资源。")

    def connect_scope(self) -> None:
        resource_name = self.resource_combo.currentText().strip()
        if not resource_name:
            self._show_warning("请先刷新资源并选择一个示波器地址。")
            return

        self._connect_request_id += 1
        connect_request_id = self._connect_request_id
        self.log(f"正在连接设备: {resource_name}")

        def task() -> tuple[KeysightOscilloscope, str]:
            scope = KeysightOscilloscope(resource_name=resource_name)
            try:
                idn = scope.connect()
                scope.assert_keysight_vendor()
                return scope, idn
            except Exception:
                scope.disconnect()
                raise

        self._run_task(
            task,
            on_success=self._on_connected,
            success_message="设备连接成功。",
            ui_guard=lambda request_id=connect_request_id: request_id == self._connect_request_id,
        )

    def _on_connected(self, result: tuple[KeysightOscilloscope, str]) -> None:
        if self.scope is not None:
            try:
                self.scope.disconnect()
            except Exception:
                pass

        self.scope, idn = result
        self.resource_combo.setCurrentText(self.scope.resource_name)
        self.status_value.setText("已连接")
        self.idn_value.setText(idn)
        self.measurement_status.setText("自动测量：未启动")
        self._refresh_auto_measure_button()
        self.log(f"实际连接地址: {self.scope.resource_name}")
        self._set_scope_display_check_enabled(True)
        self._run_task(
            self.scope.get_channel_units,
            on_success=self._update_channel_units,
            success_message="通道单位识别完成。",
            ui_guard=self._scope_ui_guard(self.scope),
        )
        self._run_task(
            lambda: self._get_scope_display_context(self.scope),
            on_success=self._on_scope_displayed_channels_loaded,
            success_message="示波器通道同步完成。",
            ui_guard=self._scope_ui_guard(self.scope),
        )
        self._request_waveform_mode_capability_hint(self.waveform_mode_combo.currentText())

    def disconnect_scope(self) -> None:
        self.stop_auto_measurement(log_message=False)
        if self.scope is None:
            return

        scope = self.scope
        self.scope = None
        self._connect_request_id += 1
        self.last_waveform_data = None
        self.last_waveform_bundle = []
        self.last_waveform_stats = None
        self.detected_channel_units = {channel: "V" for channel in SUPPORTED_CHANNELS}
        self.channel_vertical_layouts = {}
        self.export_waveform_button.setEnabled(False)
        self._set_scope_display_check_enabled(False)
        self._update_scope_display_checks([])
        self._waveform_mode_max_points_hint = ""
        self._refresh_waveform_mode_hint(self.waveform_mode_combo.currentText())
        self._sync_channel_unit_controls()
        self.status_value.setText("未连接")
        self.idn_value.setText("-")
        self.measurement_status.setText("自动测量：未启动")
        self.trigger_status_value.setText("边沿触发：未读取")
        self.trigger_event_value.setText("触发状态：未读取")
        self._refresh_auto_measure_button()
        self.waveform_summary.setText("波形状态：尚未抓取")
        self.startup_brake_dialog.reset_state()
        self._reset_waveform_visuals()
        self.log("正在断开设备连接。")
        self._run_task(scope.disconnect, success_message="设备已断开。")

    def query_system_error(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(
            scope.get_system_error,
            on_success=lambda error: self.log(f"SYST:ERR -> {error}"),
            ui_guard=self._scope_ui_guard(scope),
        )

    def autoscale(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.autoscale, success_message="AUToscale 已执行。", ui_guard=self._scope_ui_guard(scope))

    def run_scope(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.run, success_message="示波器已进入 RUN。", ui_guard=self._scope_ui_guard(scope))

    def single_acquire(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.single, success_message="示波器已进入 SINGLE 单次采集。", ui_guard=self._scope_ui_guard(scope))

    def stop_scope(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.stop, success_message="示波器已停止采集。", ui_guard=self._scope_ui_guard(scope))

    def read_trigger_settings(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(
            scope.get_edge_trigger_settings,
            on_success=self._on_trigger_settings_loaded,
            success_message="触发设置读取完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def apply_trigger_settings(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        settings = self._current_trigger_settings()
        self._run_task(
            lambda: scope.apply_edge_trigger_settings(settings),
            on_success=lambda: self._apply_trigger_settings_to_controls(settings),
            success_message="触发设置已应用。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def _on_trigger_settings_loaded(self, settings: EdgeTriggerSettings) -> None:
        self._apply_trigger_settings_to_controls(settings)

    def read_trigger_status(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(
            scope.get_trigger_event_status,
            on_success=self._on_trigger_status_loaded,
            success_message="触发状态读取完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def arm_single_trigger(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(
            scope.single,
            on_success=lambda: self.trigger_event_value.setText("触发状态：单次等待中"),
            success_message="示波器已进入单次等待触发。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def _on_trigger_status_loaded(self, triggered: bool) -> None:
        self.trigger_event_value.setText(f"触发状态：{'已触发' if triggered else '等待条件'}")

    def run_single_measurement(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        measurement_names = self._selected_measurements()
        if not measurement_names:
            self._show_warning("请至少勾选一个测量项。")
            return

        self._run_task(
            lambda: self._sync_scope_channels_and_fetch_measurements(scope, measurement_names),
            on_success=self._on_measurements_fetched_with_scope_sync,
            success_message="单次测量完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def start_auto_measurement(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        measurement_names = self._selected_measurements()
        if not measurement_names:
            self._show_warning("请至少勾选一个测量项。")
            return

        self.stop_auto_measurement(log_message=False)
        interval = max(self.interval_input.value(), 0.2)
        self._run_task(
            lambda: self._get_scope_display_context(scope),
            on_success=lambda context, captured_scope=scope, captured_names=measurement_names, captured_interval=interval: self._start_auto_measurement_with_scope_sync(
                captured_scope,
                captured_names,
                captured_interval,
                context,
            ),
            success_message="自动测量通道同步完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def stop_auto_measurement(self, log_message: bool = True) -> None:
        if self.auto_measure_handle is not None:
            self.auto_measure_handle.stop()
            self.auto_measure_handle = None
            self.measurement_status.setText("自动测量：未启动")
            self._refresh_auto_measure_button()
            if log_message:
                self.log("自动测量已停止。")

    def toggle_auto_measurement(self) -> None:
        if self.auto_measure_handle is None:
            self.start_auto_measurement()
            return
        self.stop_auto_measurement()

    def capture_screenshot(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = CAPTURE_DIR / f"scope_{timestamp}.png"
        self.log("正在抓取屏幕截图。")
        self._run_task(
            lambda: scope.capture_screenshot(target),
            on_success=self._on_screenshot_saved,
            success_message="截图保存成功。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def _on_screenshot_saved(self, image_path: Path) -> None:
        self.last_capture_path = image_path
        self.capture_value.setText(str(image_path))
        self.log(f"截图已保存: {image_path}")
        self._update_preview(image_path)

    def fetch_waveform(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        points_mode = self.waveform_mode_combo.currentText()
        points = int(self.waveform_points_input.value())
        self.log(f"开始同步示波器当前显示通道并抓取波形: {points_mode}, {points} 点。")
        self._run_task(
            lambda: self._fetch_waveforms_from_scope_display(scope, points_mode, points),
            on_success=self._on_scope_waveforms_fetched,
            success_message="波形抓取完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def refresh_waveform_detail_dialog(self) -> None:
        self.waveform_detail_dialog.show()
        self.waveform_detail_dialog.raise_()
        self.waveform_detail_dialog.activateWindow()
        self.fetch_waveform()

    def _fetch_waveforms_from_scope_display(
        self,
        scope: KeysightOscilloscope,
        points_mode: str,
        points: int,
    ) -> tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout], list[WaveformData]]:
        channels, channel_units, channel_vertical_layouts = self._get_scope_display_context(scope)
        if not channels:
            raise RuntimeError("示波器当前没有打开的通道，无法抓取波形。")
        waveforms = [scope.fetch_waveform(channel, points_mode=points_mode, points=points) for channel in channels]
        return channels, channel_units, channel_vertical_layouts, waveforms

    def _on_scope_waveforms_fetched(self, result: tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout], list[WaveformData]]) -> None:
        channels, channel_units, channel_vertical_layouts, waveforms = result
        supported_channels = [channel for channel in channels if channel in SUPPORTED_CHANNELS]
        self._update_channel_units(channel_units, log_message=False)
        self._update_channel_vertical_layouts(channel_vertical_layouts)
        requested_points = int(self.waveform_points_input.value())
        returned_points = ", ".join(
            f"{display_channel_name(waveform.channel)}={len(waveform.y_values)}"
            for waveform in waveforms
        )
        self.log(f"波形点数: 请求 {requested_points}，返回 {returned_points}")
        if supported_channels:
            primary_channel = self._apply_scope_displayed_channels(supported_channels, log_prefix="抓取前已同步示波器显示通道")
            waveforms = self._reorder_waveforms_for_primary_channel(waveforms, primary_channel)
        self._on_waveforms_fetched(waveforms)

    def sync_scope_displayed_channels(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        self.log("正在同步示波器当前显示通道。")
        self._run_task(
            lambda: self._get_scope_display_context(scope),
            on_success=self._on_scope_displayed_channels_loaded,
            success_message="示波器通道同步完成。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def _on_scope_displayed_channels_loaded(self, context: tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout]]) -> None:
        channels, channel_units, channel_vertical_layouts = context
        supported_channels = [channel for channel in channels if channel in SUPPORTED_CHANNELS]
        self._update_scope_display_checks(supported_channels)
        if not supported_channels:
            self._show_warning("示波器当前没有打开的通道。")
            return

        self._update_channel_units(channel_units, log_message=False)
        self._update_channel_vertical_layouts(channel_vertical_layouts)
        self._apply_scope_displayed_channels(supported_channels, log_prefix="已同步示波器显示通道")

    def export_waveform_csv(self) -> None:
        if not self.last_waveform_bundle:
            self._show_warning("请先抓取一次波形。")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        waveforms = list(self.last_waveform_bundle)
        if len(waveforms) == 1:
            waveform = waveforms[0]
            channel = display_channel_name(waveform.channel)
            target = WAVEFORM_DIR / f"{channel}_{waveform.points_mode}_{timestamp}.csv"
            self._run_task(
                lambda: waveform.export_csv(target),
                on_success=self._on_waveform_exported,
                success_message="波形 CSV 导出完成。",
            )
            return

        export_path = WAVEFORM_DIR / f"bundle_{timestamp}.csv"
        self._run_task(
            lambda: WaveformData.export_csv_bundle(waveforms, export_path),
            on_success=self._on_waveform_exported,
            success_message="波形 CSV 导出完成。",
        )

    def load_waveform_csv(self) -> None:
        start_dir = str(WAVEFORM_DIR if WAVEFORM_DIR.exists() else Path.cwd())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择波形 CSV",
            start_dir,
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        source_path = Path(file_path)
        self.log(f"正在加载波形 CSV: {source_path}")
        self._run_task(
            lambda: WaveformData.load_csv_bundle(source_path),
            on_success=self._on_waveforms_fetched,
            success_message="波形 CSV 加载完成。",
        )

    def _update_preview(self, image_path: Path) -> None:
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self.preview_label.setText(f"截图已保存:\n{image_path}")
            self.preview_label.setPixmap(QPixmap())
            return
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(scaled)

    def _on_waveforms_fetched(self, waveforms: list[WaveformData]) -> None:
        self._apply_fetched_waveforms(
            waveforms,
            sync_detail_dialog=self.waveform_detail_dialog.isVisible(),
            notify_startup_dialog=True,
            preserve_main_panel_view=False,
        )

    def _apply_fetched_waveforms(
        self,
        waveforms: list[WaveformData],
        *,
        sync_detail_dialog: bool,
        notify_startup_dialog: bool,
        preserve_main_panel_view: bool,
    ) -> None:
        if not waveforms:
            return
        main_panel_view_state = self.waveform_panel.capture_view_state() if preserve_main_panel_view else None
        preferred_primary_channel = None
        if preserve_main_panel_view:
            selected_channel = self._selected_channel()
            available_channels = {waveform.channel for waveform in waveforms}
            if selected_channel in available_channels:
                preferred_primary_channel = selected_channel
                waveforms = self._reorder_waveforms_for_primary_channel(list(waveforms), preferred_primary_channel)
        primary_waveform = waveforms[0]
        self.last_waveform_bundle = list(waveforms)
        self.last_waveform_data = primary_waveform
        self.last_waveform_stats = primary_waveform.analyze()
        if notify_startup_dialog:
            self.startup_brake_dialog.handle_waveforms_updated()
        if preserve_main_panel_view and preferred_primary_channel is not None:
            self._update_scope_display_checks([waveform.channel for waveform in waveforms if waveform.channel in SUPPORTED_CHANNELS])
        else:
            self._sync_waveform_channel_selection(waveforms)
        self.export_waveform_button.setEnabled(True)
        self.waveform_summary.setText(
            "波形状态："
            f"{' + '.join(display_channel_name(waveform.channel) for waveform in waveforms)} / {primary_waveform.points_mode} / "
            f"主通道 {display_channel_name(primary_waveform.channel)} / {len(primary_waveform.x_values)} 点 / "
            f"时间跨度 {self.last_waveform_stats.duration_s:.6e}s / "
            f"{self._waveform_range_label(primary_waveform.channel)} "
            f"{self.last_waveform_stats.voltage_min:.4f}{self._channel_unit(primary_waveform.channel)} ~ "
            f"{self.last_waveform_stats.voltage_max:.4f}{self._channel_unit(primary_waveform.channel)}"
        )
        self.waveform_panel.set_waveforms(waveforms, self.last_waveform_stats)
        if self.channel_vertical_layouts:
            self.waveform_panel.set_scope_vertical_layouts(self.channel_vertical_layouts)
        if preserve_main_panel_view:
            self.waveform_panel.restore_view_state(main_panel_view_state)
        if sync_detail_dialog:
            self.sync_waveform_detail_dialog()

    def _on_waveform_exported(self, csv_path: Path) -> None:
        self.waveform_summary.setText(f"波形状态：已导出 {csv_path}")
        self.log(f"波形 CSV 已保存: {csv_path}")

    def _reset_waveform_visuals(self) -> None:
        self.waveform_panel.clear()
        self.waveform_detail_dialog.clear()

    def show_waveform_detail_dialog(self) -> None:
        self.waveform_detail_dialog.show()
        self.waveform_detail_dialog.raise_()
        self.waveform_detail_dialog.activateWindow()
        self.sync_waveform_detail_dialog()

    def show_startup_brake_dialog(self) -> None:
        self.startup_brake_dialog.show_dialog()

    def sync_waveform_detail_dialog(self) -> None:
        if not self.last_waveform_bundle or self.last_waveform_stats is None:
            self._show_warning("当前还没有波形数据可同步。")
            return
        self.waveform_detail_dialog.set_waveforms(self.last_waveform_bundle, self.last_waveform_stats)
        if self.channel_vertical_layouts:
            self.waveform_detail_dialog.set_scope_vertical_layouts(self.channel_vertical_layouts)
        self.waveform_detail_dialog.show()
        self.waveform_detail_dialog.raise_()
        self.waveform_detail_dialog.activateWindow()

    def _update_measurements(self, results) -> None:
        updated_at = datetime.now().strftime("%H:%M:%S")
        self.last_update_value.setText(f"最近更新：{updated_at}")
        self.result_table.setRowCount(len(results))
        for row, result in enumerate(results):
            self.result_table.setItem(row, 0, QTableWidgetItem(result.label))
            self.result_table.setItem(row, 1, QTableWidgetItem(result.display_value))
            self.result_table.setItem(row, 2, QTableWidgetItem(result.unit))
            self.result_table.setItem(row, 3, QTableWidgetItem(f"{result.raw_value:.6e}"))
            self.result_table.setItem(row, 4, QTableWidgetItem(updated_at))

    def _update_measurement_count(self) -> None:
        count = len(self._selected_measurements())
        self.measurement_count_label.setText(f"已选 {count} 项")

    def _refresh_auto_measure_button(self) -> None:
        if self.auto_measure_handle is None:
            self.auto_measure_button.setText("启动自动测量")
        else:
            self.auto_measure_button.setText("停止自动测量")

    def _select_default_measurements(self) -> None:
        for name, checkbox in self.measurement_checks.items():
            checkbox.setChecked(name in DEFAULT_MEASUREMENT_SET)
        self._update_measurement_count()

    def _select_all_measurements(self) -> None:
        for checkbox in self.measurement_checks.values():
            checkbox.setChecked(True)
        self._update_measurement_count()

    def _clear_measurements(self) -> None:
        for checkbox in self.measurement_checks.values():
            checkbox.setChecked(False)
        self._update_measurement_count()

    def _apply_measurement_template(self, template_name: str) -> None:
        selected = MEASUREMENT_TEMPLATES[template_name]
        for name, checkbox in self.measurement_checks.items():
            checkbox.setChecked(name in selected)
        self._update_measurement_count()
        self.log(f"已应用测量模板: {template_name}")

    def _refresh_waveform_mode_hint(self, mode: str) -> None:
        hint = WAVEFORM_MODE_HINTS.get(mode, "")
        if self._waveform_mode_max_points_hint:
            hint = f"{hint}\n{self._waveform_mode_max_points_hint}" if hint else self._waveform_mode_max_points_hint
        self.waveform_mode_combo.setToolTip(hint)
        self.waveform_mode_hint_label.setText(hint)

    def _on_waveform_mode_changed(self, mode: str) -> None:
        default_points = WAVEFORM_MODE_DEFAULT_POINTS.get(mode)
        if default_points is not None:
            self.waveform_points_input.setValue(default_points)
        self._waveform_mode_max_points_hint = ""
        self._refresh_waveform_mode_hint(mode)
        self._request_waveform_mode_capability_hint(mode)
        self._save_ui_state()

    def _request_waveform_mode_capability_hint(self, mode: str) -> None:
        scope = self.scope
        if scope is None or not scope.is_connected:
            return
        self._run_task(
            lambda captured_scope=scope, captured_mode=mode: self._query_waveform_mode_capability_hint(captured_scope, captured_mode),
            on_success=self._apply_waveform_mode_capability_hint,
            ui_guard=self._scope_ui_guard(scope),
        )

    def _query_waveform_mode_capability_hint(
        self,
        scope: KeysightOscilloscope,
        mode: str,
    ) -> tuple[str, str]:
        channels = scope.get_displayed_channels()
        if not channels:
            channels = [self._selected_channel()]
        primary_channel = self._choose_primary_channel_from_displayed(channels)
        max_points = scope.get_max_waveform_points(primary_channel, points_mode=mode)
        return mode, f"当前示波器该模式可接受点数上限约为 {max_points} 点（基于 {display_channel_name(primary_channel)} 查询）"

    def _apply_waveform_mode_capability_hint(self, result: tuple[str, str]) -> None:
        mode, hint = result
        if self.waveform_mode_combo.currentText() != mode:
            return
        self._waveform_mode_max_points_hint = hint
        self._refresh_waveform_mode_hint(mode)

    def _selected_measurements(self) -> list[str]:
        return [name for name, checkbox in self.measurement_checks.items() if checkbox.isChecked()]

    def _sync_waveform_channel_selection(self, waveforms: list[WaveformData]) -> None:
        supported_channels = [waveform.channel for waveform in waveforms if waveform.channel in SUPPORTED_CHANNELS]
        if not supported_channels:
            return

        primary_channel = self._choose_primary_channel_from_displayed(supported_channels)
        self.channel_combo.blockSignals(True)
        self._set_selected_channel(primary_channel)
        self.channel_combo.blockSignals(False)
        self._update_scope_display_checks(supported_channels)

    def _selected_channel(self) -> str:
        current = self.channel_combo.currentData()
        if isinstance(current, str) and current:
            return current
        return normalize_channel_name(self.channel_combo.currentText())

    def _set_selected_channel(self, channel: str) -> None:
        index = self.channel_combo.findData(normalize_channel_name(channel))
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)

    def _choose_primary_channel_from_displayed(self, channels: list[str]) -> str:
        selected_channel = self._selected_channel()
        if selected_channel in channels:
            return selected_channel
        return channels[0]

    def _reorder_waveforms_for_primary_channel(
        self,
        waveforms: list[WaveformData],
        primary_channel: str,
    ) -> list[WaveformData]:
        primary_waveforms = [waveform for waveform in waveforms if waveform.channel == primary_channel]
        if not primary_waveforms:
            return waveforms
        return primary_waveforms + [waveform for waveform in waveforms if waveform.channel != primary_channel]

    def _apply_scope_displayed_channels(self, channels: list[str], *, log_prefix: str) -> str:
        primary_channel = self._choose_primary_channel_from_displayed(channels)
        self.channel_combo.blockSignals(True)
        self._set_selected_channel(primary_channel)
        self.channel_combo.blockSignals(False)
        self._update_scope_display_checks(channels)
        self.log(
            f"{log_prefix}: " + ", ".join(display_channel_name(channel) for channel in channels)
        )
        return primary_channel

    def _get_scope_display_context(
        self,
        scope: KeysightOscilloscope,
    ) -> tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout]]:
        channels = scope.get_displayed_channels()
        channel_units = scope.get_channel_units(channels)
        channel_vertical_layouts = scope.get_channel_vertical_layouts(channels)
        return channels, channel_units, channel_vertical_layouts

    def _sync_scope_channels_and_fetch_measurements(
        self,
        scope: KeysightOscilloscope,
        measurement_names: list[str],
    ):
        channels, channel_units, channel_vertical_layouts = self._get_scope_display_context(scope)
        if not channels:
            raise RuntimeError("示波器当前没有打开的通道，无法执行测量。")
        measurement_channel = self._choose_primary_channel_from_displayed(channels)
        results = scope.fetch_measurements(measurement_channel, measurement_names)
        return channels, channel_units, channel_vertical_layouts, results

    def _on_measurements_fetched_with_scope_sync(self, result) -> None:
        channels, channel_units, channel_vertical_layouts, measurements = result
        supported_channels = [channel for channel in channels if channel in SUPPORTED_CHANNELS]
        self._update_channel_units(channel_units, log_message=False)
        self._update_channel_vertical_layouts(channel_vertical_layouts)
        if supported_channels:
            primary_channel = self._apply_scope_displayed_channels(
                supported_channels,
                log_prefix="测量前已同步示波器显示通道",
            )
            self.log(f"执行单次测量: {display_channel_name(primary_channel)}")
        self._update_measurements(measurements)

    def _start_auto_measurement_with_scope_sync(
        self,
        scope: KeysightOscilloscope,
        measurement_names: list[str],
        interval: float,
        context: tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout]],
    ) -> None:
        channels, channel_units, channel_vertical_layouts = context
        supported_channels = [channel for channel in channels if channel in SUPPORTED_CHANNELS]
        if not supported_channels:
            self._show_warning("示波器当前没有打开的通道，无法启动自动测量。")
            return

        self._update_channel_units(channel_units, log_message=False)
        self._update_channel_vertical_layouts(channel_vertical_layouts)
        channel = self._apply_scope_displayed_channels(
            supported_channels,
            log_prefix="自动测量前已同步示波器显示通道",
        )
        self.log(f"自动测量已启动，主通道 {display_channel_name(channel)}，间隔 {interval:.1f}s。")
        self.measurement_status.setText(f"自动测量：运行中 ({interval:.1f}s)")
        self.auto_measure_handle = self.task_runner.run_repeating(
            lambda: self._sync_scope_channels_and_fetch_measurements(scope, measurement_names),
            interval_s=interval,
            on_result=self._on_auto_measurements_fetched_with_scope_sync,
            on_error=self._handle_auto_measurement_error,
        )
        self._refresh_auto_measure_button()

    def _on_auto_measurements_fetched_with_scope_sync(self, result) -> None:
        channels, channel_units, channel_vertical_layouts, measurements = result
        supported_channels = [channel for channel in channels if channel in SUPPORTED_CHANNELS]
        self._update_channel_units(channel_units, log_message=False)
        self._update_channel_vertical_layouts(channel_vertical_layouts)
        if supported_channels:
            primary_channel = self._apply_scope_displayed_channels(
                supported_channels,
                log_prefix="自动测量已同步示波器显示通道",
            )
            self.measurement_status.setText(
                f"自动测量：运行中 ({display_channel_name(primary_channel)})"
            )
        self._update_measurements(measurements)

    def _channel_unit(self, channel: str) -> str:
        override = self.channel_unit_overrides.get(channel)
        if override in {"V", "A"}:
            return override
        return self.detected_channel_units.get(channel, "V")

    def _waveform_range_label(self, channel: str) -> str:
        return "电流范围" if self._channel_unit(channel) == "A" else "电压范围"

    def _update_channel_units(self, channel_units: dict[str, str], *, log_message: bool = True) -> None:
        updated_units = {
            channel: channel_units.get(channel, self.detected_channel_units.get(channel, "V"))
            for channel in SUPPORTED_CHANNELS
        }
        self.detected_channel_units = updated_units
        self._sync_channel_unit_controls()
        if log_message:
            self.log(
                "通道单位识别: "
                + ", ".join(
                    f"{display_channel_name(channel)}={self._channel_unit_status_text(channel)}"
                    for channel in SUPPORTED_CHANNELS
                )
            )

    def _sync_channel_unit_controls(self) -> None:
        for channel, combo in self.channel_unit_combos.items():
            detected = self.detected_channel_units.get(channel, "V")
            combo.blockSignals(True)
            combo.setItemText(0, f"自动({detected})")
            override = self.channel_unit_overrides.get(channel)
            if override in {"V", "A"}:
                index = combo.findData(override)
            else:
                index = combo.findData(None)
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)

    def _set_channel_unit_override(self, channel: str, override: str | None) -> None:
        normalized = override if override in {"V", "A"} else None
        self.channel_unit_overrides[channel] = normalized
        self._sync_channel_unit_controls()
        self.log(f"{display_channel_name(channel)} 单位已切换为 {self._channel_unit_status_text(channel)}。")
        self._update_waveform_unit_views()

    def _channel_unit_status_text(self, channel: str) -> str:
        override = self.channel_unit_overrides.get(channel)
        detected = self.detected_channel_units.get(channel, "V")
        if override in {"V", "A"}:
            return f"{override}（手动）"
        return f"{detected}（自动）"

    def _update_waveform_unit_views(self) -> None:
        if self.last_waveform_bundle:
            self.waveform_panel.set_waveforms(self.last_waveform_bundle, self.last_waveform_stats)
            primary_waveform = self.last_waveform_bundle[0]
            if self.last_waveform_stats is not None:
                self.waveform_summary.setText(
                    f"波形状态：已加载 {len(self.last_waveform_bundle)} 路，主通道 {display_channel_name(primary_waveform.channel)}，"
                    f"点数 {self.last_waveform_stats.point_count}，范围 "
                    f"{self.last_waveform_stats.voltage_min:.4f}{self._channel_unit(primary_waveform.channel)} ~ "
                    f"{self.last_waveform_stats.voltage_max:.4f}{self._channel_unit(primary_waveform.channel)}"
                )
            self.waveform_detail_dialog.set_waveforms(self.last_waveform_bundle, self.last_waveform_stats)

    def _update_channel_vertical_layouts(self, channel_vertical_layouts: dict[str, ChannelVerticalLayout]) -> None:
        self.channel_vertical_layouts = {
            channel: {"scale": layout.scale, "offset": layout.offset}
            for channel, layout in channel_vertical_layouts.items()
        }

    def _set_scope_display_check_enabled(self, enabled: bool) -> None:
        for checkbox in self.scope_display_checks.values():
            checkbox.setEnabled(enabled)

    def _update_scope_display_checks(self, channels: list[str]) -> None:
        active_channels = set(channels)
        self._updating_scope_display_checks = True
        try:
            for channel, checkbox in self.scope_display_checks.items():
                checkbox.setChecked(channel in active_channels)
        finally:
            self._updating_scope_display_checks = False

    def _toggle_scope_channel_display(self, channel: str, enabled: bool) -> None:
        if self._updating_scope_display_checks:
            return
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self.log(f"正在设置 {display_channel_name(channel)} {'显示' if enabled else '隐藏'}。")
        self._run_task(
            lambda: self._set_scope_channel_display_and_reload(scope, channel, enabled),
            on_success=self._on_scope_displayed_channels_loaded,
            success_message="示波器通道状态已更新。",
            ui_guard=self._scope_ui_guard(scope),
        )

    def _set_scope_channel_display_and_reload(
        self,
        scope: KeysightOscilloscope,
        channel: str,
        enabled: bool,
    ) -> tuple[list[str], dict[str, str], dict[str, ChannelVerticalLayout]]:
        scope.set_channel_display(channel, enabled)
        return self._get_scope_display_context(scope)

    def _get_scope_or_warn(self) -> KeysightOscilloscope | None:
        if self.scope is None or not self.scope.is_connected:
            self._show_warning("请先连接示波器。")
            return None
        return self.scope

    def _scope_ui_guard(self, scope: KeysightOscilloscope):
        return lambda current_scope=scope: self.scope is current_scope and current_scope.is_connected

    def _resource_selected(self, index: int) -> None:
        if index >= 0:
            self.resource_combo.setCurrentIndex(index)

    def _run_task(self, task, on_success=None, success_message: str | None = None, ui_guard=None) -> None:
        def handle_success(result) -> None:
            if ui_guard is not None and not ui_guard():
                return
            if on_success is not None:
                on_success(result)
            if success_message:
                self.log(success_message)

        def handle_error(error: Exception) -> None:
            if ui_guard is not None and not ui_guard():
                return
            self._handle_error(error)

        self.task_runner.run(task, on_success=handle_success, on_error=handle_error)

    def _handle_error(self, error: Exception) -> None:
        self.log(f"操作失败: {error}")
        QMessageBox.critical(self, "操作失败", str(error))

    def _handle_auto_measurement_error(self, error: Exception) -> None:
        self.stop_auto_measurement(log_message=False)
        self._handle_error(error)

    def _show_warning(self, message: str) -> None:
        self.log(message)
        QMessageBox.warning(self, "提示", message)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.stop_auto_measurement(log_message=False)
        if self.scope is not None:
            try:
                self.scope.disconnect()
            except Exception:
                pass
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.last_capture_path and self.last_capture_path.exists():
            self._update_preview(self.last_capture_path)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = ScopeMainWindow()
    window.show()
    app.exec()
