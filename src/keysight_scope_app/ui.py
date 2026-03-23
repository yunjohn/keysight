from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
import sys
import threading

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

from keysight_scope_app.instrument import (
    MEASUREMENT_DEFINITIONS,
    SUPPORTED_CHANNELS,
    SUPPORTED_WAVEFORM_POINTS_MODES,
    KeysightOscilloscope,
    WaveformData,
    WaveformStats,
    list_visa_resources,
)
from keysight_scope_app.startup_brake_dialog import StartupBrakeTestDialog
from keysight_scope_app.waveform_dialog import WaveformDetailDialog
from keysight_scope_app.waveform_panel import WaveformAnalysisPanel


CAPTURE_DIR = Path("captures")
WAVEFORM_DIR = Path("captures") / "waveforms"
MAX_LOG_LINES = 300
DEFAULT_MEASUREMENT_SET = {"频率", "峰峰值", "均方根"}
MEASUREMENT_TEMPLATES = {
    "基础模板": {"频率", "周期", "峰峰值", "均方根"},
    "方波模板": {"频率", "周期", "峰峰值", "占空比", "正脉宽", "负脉宽", "上升时间", "下降时间"},
    "纹波模板": {"峰峰值", "均方根", "最大值", "最小值"},
    "边沿模板": {"最大值", "最小值", "高电平估计", "低电平估计", "上升时间", "下降时间"},
}


def _display_channel_name(channel: str) -> str:
    if channel.startswith("CHANnel"):
        return channel.replace("CHANnel", "CH", 1)
    return channel


def _normalize_channel_name(channel: str) -> str:
    normalized = channel.strip()
    if normalized.upper().startswith("CH") and normalized[2:].isdigit():
        return f"CHANnel{normalized[2:]}"
    return normalized


class ScopeMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.scope: KeysightOscilloscope | None = None
        self.auto_measure_stop: threading.Event | None = None
        self.ui_queue: Queue = Queue()
        self.log_lines: list[str] = []
        self.measurement_checks: dict[str, QCheckBox] = {}
        self.overlay_channel_checks: dict[str, QCheckBox] = {}
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

        measure_box = self._group_box("采集与测量")
        measure_layout = QVBoxLayout(measure_box)
        top_row = QHBoxLayout()

        self.channel_combo = QComboBox()
        for channel in SUPPORTED_CHANNELS:
            self.channel_combo.addItem(_display_channel_name(channel), channel)
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

        overlay_row = QHBoxLayout()
        overlay_row.addWidget(QLabel("叠加通道"))
        for channel in SUPPORTED_CHANNELS:
            checkbox = QCheckBox(_display_channel_name(channel))
            self.overlay_channel_checks[channel] = checkbox
            overlay_row.addWidget(checkbox)
        overlay_row.addStretch(1)
        measure_layout.addLayout(overlay_row)

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
        self.detach_waveform_button = QPushButton("独立显示")
        self.sync_waveform_button = QPushButton("同步到独立窗")
        self.open_startup_brake_button = QPushButton("启动刹车测试")
        waveform_toolbar.addWidget(self.detach_waveform_button)
        waveform_toolbar.addWidget(self.sync_waveform_button)
        waveform_toolbar.addWidget(self.open_startup_brake_button)
        waveform_toolbar.addStretch(1)
        waveform_layout.addLayout(waveform_toolbar)

        self.waveform_panel = WaveformAnalysisPanel(self, compact_mode=True)
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
        self.resource_combo.activated.connect(self._resource_selected)
        self.channel_combo.currentTextChanged.connect(lambda _: self._refresh_overlay_channel_checks())
        self.select_default_button.clicked.connect(self._select_default_measurements)
        self.select_all_button.clicked.connect(self._select_all_measurements)
        self.clear_selection_button.clicked.connect(self._clear_measurements)
        self.fetch_waveform_button.clicked.connect(self.fetch_waveform)
        self.load_waveform_button.clicked.connect(self.load_waveform_csv)
        self.export_waveform_button.clicked.connect(self.export_waveform_csv)
        self.detach_waveform_button.clicked.connect(self.show_waveform_detail_dialog)
        self.sync_waveform_button.clicked.connect(self.sync_waveform_detail_dialog)
        self.open_startup_brake_button.clicked.connect(self.show_startup_brake_dialog)
        self._refresh_overlay_channel_checks()
        self._stabilize_push_buttons(self)
        self._normalize_label_alignment(self)
        self._update_measurement_count()
        self._refresh_auto_measure_button()

    def _build_timer(self) -> None:
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._drain_ui_queue)
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

        self._run_task(task, on_success=self._on_connected, success_message="设备连接成功。")

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

    def disconnect_scope(self) -> None:
        self.stop_auto_measurement(log_message=False)
        if self.scope is None:
            return

        scope = self.scope
        self.scope = None
        self.last_waveform_data = None
        self.last_waveform_bundle = []
        self.last_waveform_stats = None
        self.export_waveform_button.setEnabled(False)
        self.status_value.setText("未连接")
        self.idn_value.setText("-")
        self.measurement_status.setText("自动测量：未启动")
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
        self._run_task(scope.get_system_error, on_success=lambda error: self.log(f"SYST:ERR -> {error}"))

    def autoscale(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.autoscale, success_message="AUToscale 已执行。")

    def run_scope(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.run, success_message="示波器已进入 RUN。")

    def single_acquire(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.single, success_message="示波器已进入 SINGLE 单次采集。")

    def stop_scope(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return
        self._run_task(scope.stop, success_message="示波器已停止采集。")

    def run_single_measurement(self) -> None:
        scope = self._get_scope_or_warn()
        if scope is None:
            return

        measurement_names = self._selected_measurements()
        if not measurement_names:
            self._show_warning("请至少勾选一个测量项。")
            return

        channel = self._selected_channel()
        self.log(f"执行单次测量: {channel} / {', '.join(measurement_names)}")
        self._run_task(
            lambda: scope.fetch_measurements(channel, measurement_names),
            on_success=self._update_measurements,
            success_message="单次测量完成。",
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
        stop_event = threading.Event()
        self.auto_measure_stop = stop_event
        channel = self._selected_channel()
        interval = max(self.interval_input.value(), 0.2)
        self.log(f"自动测量已启动，间隔 {interval:.1f}s。")
        self.measurement_status.setText(f"自动测量：运行中 ({interval:.1f}s)")
        self._refresh_auto_measure_button()

        def worker() -> None:
            while not stop_event.is_set():
                try:
                    results = scope.fetch_measurements(channel, measurement_names)
                    self._post_ui(lambda data=results: self._update_measurements(data))
                except Exception as exc:
                    self._post_ui(lambda error=exc: self._handle_auto_measurement_error(error))
                    stop_event.set()
                    break
                if stop_event.wait(interval):
                    break

        threading.Thread(target=worker, daemon=True).start()

    def stop_auto_measurement(self, log_message: bool = True) -> None:
        if self.auto_measure_stop is not None:
            self.auto_measure_stop.set()
            self.auto_measure_stop = None
            self.measurement_status.setText("自动测量：未启动")
            self._refresh_auto_measure_button()
            if log_message:
                self.log("自动测量已停止。")

    def toggle_auto_measurement(self) -> None:
        if self.auto_measure_stop is None:
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

        channels = self._selected_waveform_channels()
        points_mode = self.waveform_mode_combo.currentText()
        points = int(self.waveform_points_input.value())
        self.log(f"开始抓取波形: {', '.join(_display_channel_name(channel) for channel in channels)}, {points_mode}, {points} 点。")
        self._run_task(
            lambda: [scope.fetch_waveform(channel, points_mode=points_mode, points=points) for channel in channels],
            on_success=self._on_waveforms_fetched,
            success_message="波形抓取完成。",
        )

    def export_waveform_csv(self) -> None:
        if not self.last_waveform_bundle:
            self._show_warning("请先抓取一次波形。")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        waveforms = list(self.last_waveform_bundle)
        if len(waveforms) == 1:
            waveform = waveforms[0]
            channel = _display_channel_name(waveform.channel)
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
        if not waveforms:
            return
        primary_waveform = waveforms[0]
        self.last_waveform_bundle = list(waveforms)
        self.last_waveform_data = primary_waveform
        self.last_waveform_stats = primary_waveform.analyze()
        self.startup_brake_dialog.handle_waveforms_updated()
        self._sync_waveform_channel_selection(waveforms)
        self.export_waveform_button.setEnabled(True)
        self.waveform_summary.setText(
            "波形状态："
            f"{' + '.join(_display_channel_name(waveform.channel) for waveform in waveforms)} / {primary_waveform.points_mode} / "
            f"主通道 {_display_channel_name(primary_waveform.channel)} / {len(primary_waveform.x_values)} 点 / "
            f"时间跨度 {self.last_waveform_stats.duration_s:.6e}s / "
            f"电压范围 {self.last_waveform_stats.voltage_min:.4f}V ~ {self.last_waveform_stats.voltage_max:.4f}V"
        )
        self.waveform_panel.set_waveforms(waveforms, self.last_waveform_stats)
        if self.waveform_detail_dialog.isVisible():
            self.waveform_detail_dialog.set_waveforms(waveforms, self.last_waveform_stats)

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
        if self.auto_measure_stop is None:
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

    def _selected_measurements(self) -> list[str]:
        return [name for name, checkbox in self.measurement_checks.items() if checkbox.isChecked()]

    def _selected_waveform_channels(self) -> list[str]:
        primary_channel = self._selected_channel()
        channels = [primary_channel]
        for channel, checkbox in self.overlay_channel_checks.items():
            if channel == primary_channel:
                continue
            if checkbox.isChecked():
                channels.append(channel)
        return channels

    def _refresh_overlay_channel_checks(self, selected_channels: set[str] | None = None) -> None:
        primary_channel = self._selected_channel()
        for channel, checkbox in self.overlay_channel_checks.items():
            is_primary = channel == primary_channel
            checkbox.blockSignals(True)
            if is_primary:
                checkbox.setChecked(False)
            elif selected_channels is not None:
                checkbox.setChecked(channel in selected_channels)
            checkbox.setEnabled(not is_primary)
            checkbox.blockSignals(False)

    def _sync_waveform_channel_selection(self, waveforms: list[WaveformData]) -> None:
        supported_channels = [waveform.channel for waveform in waveforms if waveform.channel in SUPPORTED_CHANNELS]
        if not supported_channels:
            return

        primary_channel = supported_channels[0]
        self.channel_combo.blockSignals(True)
        self._set_selected_channel(primary_channel)
        self.channel_combo.blockSignals(False)
        self._refresh_overlay_channel_checks(set(supported_channels[1:]))

    def _selected_channel(self) -> str:
        current = self.channel_combo.currentData()
        if isinstance(current, str) and current:
            return current
        return _normalize_channel_name(self.channel_combo.currentText())

    def _set_selected_channel(self, channel: str) -> None:
        index = self.channel_combo.findData(_normalize_channel_name(channel))
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)

    def _get_scope_or_warn(self) -> KeysightOscilloscope | None:
        if self.scope is None or not self.scope.is_connected:
            self._show_warning("请先连接示波器。")
            return None
        return self.scope

    def _resource_selected(self, index: int) -> None:
        if index >= 0:
            self.resource_combo.setCurrentIndex(index)

    def _run_task(self, task, on_success=None, success_message: str | None = None) -> None:
        def worker() -> None:
            try:
                result = task()
            except Exception as exc:
                self._post_ui(lambda error=exc: self._handle_error(error))
                return

            def finish() -> None:
                if on_success is not None:
                    on_success(result)
                if success_message:
                    self.log(success_message)

            self._post_ui(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _post_ui(self, callback) -> None:
        self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except Empty:
                break
            callback()

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
