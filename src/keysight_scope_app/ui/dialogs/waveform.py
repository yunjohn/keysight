from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSettings, QTimer, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QListWidget,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QToolBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.analysis.waveform import WaveformData, WaveformStats
from keysight_scope_app.device.instrument import SUPPORTED_CHANNELS
from keysight_scope_app.ui.helpers import (
    apply_responsive_window_geometry,
    create_scroll_area,
    display_channel_name,
    set_equal_button_widths,
    set_uniform_control_height,
)
from keysight_scope_app.ui.panels.waveform import WaveformAnalysisPanel
from keysight_scope_app.ui.waveform_theme import INSTRUMENT_DARK_THEME, InteractionTool
from keysight_scope_app.ui.waveform_navigator import WaveformNavigator
from keysight_scope_app.utils import format_engineering_value
from keysight_scope_app.services.quality import inspect_waveforms
from keysight_scope_app.services.comparison import compare_to_baseline
from keysight_scope_app.services.waveform_workspace import (
    ViewHistory,
    WaveformAnnotation,
    WaveformViewState,
    detect_quick_events,
    save_workspace_metadata,
)


WAVEFORM_CONFIG_DIR = Path("captures") / "waveforms"
WAVEFORM_MEASUREMENT_SETTINGS_PATH = WAVEFORM_CONFIG_DIR / "waveform_measurements.json"
WAVEFORM_PHASE_SETTINGS_PATH = WAVEFORM_CONFIG_DIR / "waveform_phase_settings.json"
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
OVERLAY_TITLE_POINT_SIZE = 10
OVERLAY_BODY_POINT_SIZE = 9
OVERLAY_TITLE_HTML_PX = 14
OVERLAY_BODY_HTML_PX = 12
WAVEFORM_SIDEBAR_WIDTH = 400
WAVEFORM_SIDEBAR_TAB_HEIGHT = 36
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


def _waveform_workspace_stylesheet() -> str:
    theme = INSTRUMENT_DARK_THEME
    return f"""
        QDialog, QWidget {{ background: {theme.window_background}; color: {theme.primary_text}; }}
        QGroupBox, QFrame#sidebarCard {{
            background: {theme.panel_background};
            border: 1px solid {theme.border};
            border-radius: 7px;
            margin-top: 7px;
        }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 9px; padding: 0 4px; }}
        QPushButton, QToolButton, QComboBox {{
            background: {theme.grid_minor};
            color: {theme.primary_text};
            border: 1px solid {theme.border};
            border-radius: 5px;
            padding: 5px 9px;
        }}
        QPushButton:hover, QToolButton:hover {{ background: {theme.grid_major}; border-color: {theme.crosshair}; }}
        QPushButton:checked, QToolButton:checked {{ background: {theme.border}; border-color: {theme.channels["CHANnel3"].color}; }}
        QPushButton:disabled, QToolButton:disabled {{ color: {theme.border}; background: {theme.panel_background}; }}
        QToolBox::tab {{
            background: {theme.grid_minor};
            color: {theme.secondary_text};
            border: 1px solid {theme.border};
            border-radius: 4px;
            padding: 5px 9px;
            min-height: 24px;
            font-weight: 600;
        }}
        QToolBox::tab:selected {{ background: {theme.border}; color: {theme.primary_text}; }}
        QListWidget {{ background: {theme.plot_background}; border: 1px solid {theme.border}; border-radius: 4px; }}
        QListWidget::item:selected {{ background: {theme.grid_major}; }}
        QMenu {{ background: {theme.panel_background}; color: {theme.primary_text}; border: 1px solid {theme.border}; }}
        QMenu::item:selected {{ background: {theme.grid_major}; }}
    """


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
        self.theme = INSTRUMENT_DARK_THEME
        self.interaction_tool = InteractionTool.ZOOM
        self.setStyleSheet(_waveform_workspace_stylesheet())
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.setWindowTitle("独立波形显示")
        apply_responsive_window_geometry(
            self,
            minimum_width=760,
            minimum_height=540,
            preferred_width=1440,
            preferred_height=920,
        )
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        content = QWidget(self)
        layout = QVBoxLayout(content)
        outer_layout.addWidget(create_scroll_area(self, content, minimum_px=1120, minimum_chars=132))
        self.channel_visibility_checks: dict[str, QCheckBox] = {}
        self.current_waveforms: list[WaveformData] = []
        self.measurement_config: dict[str, set[str]] = {}
        self.cursor_measurements: dict[str, str] = {}
        self._phase_interval_memory: dict[str, float | None] = {}
        self._updating_channel_checks = False
        self._link_scope_channels = False
        self._measurement_frozen = False
        self._applying_saved_phase_interval = False
        self._view_history = ViewHistory()
        self._bookmarks: dict[str, WaveformViewState] = {}
        self._annotations: list[WaveformAnnotation] = []
        self._settings = QSettings("KeysightScopeApp", "WaveformDetail")
        self._restoring_view = False
        self._view_history_timer = QTimer(self)
        self._view_history_timer.setSingleShot(True)
        self._view_history_timer.setInterval(180)
        self._view_history_timer.timeout.connect(self._record_current_view)
        self._load_measurement_config()
        self._load_phase_settings()

        toolbar_container = QWidget(content)
        toolbar = QVBoxLayout(toolbar_container)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self.refresh_waveform_button = QPushButton("抓取")
        self.refresh_waveform_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_waveform_button.setToolTip("从示波器单次抓取波形")
        self.refresh_waveform_button.clicked.connect(self._request_waveform_refresh)
        self.reset_waveform_button = QPushButton("全图")
        self.reset_waveform_button.setIcon(self.style().standardIcon(QStyle.SP_DialogResetButton))
        self.reset_waveform_button.setToolTip("恢复完整波形视图（F，双击图表）")
        self.reset_waveform_button.clicked.connect(self._reset_waveform_view)
        self.undo_view_button = QPushButton("后退")
        self.undo_view_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        self.undo_view_button.setToolTip("返回上一个视图（Ctrl+Z）")
        self.undo_view_button.clicked.connect(self._undo_view)
        self.redo_view_button = QPushButton("前进")
        self.redo_view_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowForward))
        self.redo_view_button.setToolTip("前进到下一个视图（Ctrl+Y）")
        self.redo_view_button.clicked.connect(self._redo_view)
        self.sidebar_toggle_button = QPushButton("隐藏侧栏")
        self.sidebar_toggle_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.sidebar_toggle_button.setToolTip("显示或隐藏分析侧栏")
        self.sidebar_toggle_button.setCheckable(True)
        self.sidebar_toggle_button.toggled.connect(self._toggle_sidebar)
        self.export_current_view_button = QPushButton("导出当前视图")
        self.export_current_view_button.clicked.connect(self._export_current_view_bundle)
        self.export_cursor_ab_button = QPushButton("导出游标A-B")
        self.export_cursor_ab_button.clicked.connect(self._export_cursor_ab_bundle)
        self.capture_current_view_button = QPushButton("截图")
        self.capture_current_view_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.capture_current_view_button.setToolTip("导出当前主题下的波形截图")
        self.capture_current_view_button.clicked.connect(self._capture_current_view_image)
        self.freeze_measurements_button = QPushButton("冻结测量")
        self.freeze_measurements_button.setCheckable(True)
        self.freeze_measurements_button.toggled.connect(self._toggle_measurement_freeze)
        self.measurement_scope_combo = QComboBox()
        self.measurement_scope_combo.addItem("当前视图", "view")
        self.measurement_scope_combo.addItem("游标 A-B", "cursor")
        self.measurement_scope_combo.addItem("整条波形", "full")
        self.measurement_scope_combo.currentIndexChanged.connect(self._refresh_measurement_footer)
        self.measurement_settings_button = QPushButton("测量项设置")
        self.measurement_settings_button.clicked.connect(self._show_measurement_settings)
        self.tool_button_group = QButtonGroup(self)
        self.tool_button_group.setExclusive(True)
        self.tool_buttons: dict[InteractionTool, QPushButton] = {}
        for tool, text, tooltip in (
            (InteractionTool.ZOOM, "缩放", "左键框选缩放；Shift 临时平移"),
            (InteractionTool.PAN, "平移", "左键拖动时间轴"),
            (InteractionTool.CURSOR_A, "游标 A", "在图表中放置游标 A（A）"),
            (InteractionTool.CURSOR_B, "游标 B", "在图表中放置游标 B（B）"),
            (InteractionTool.ANNOTATE, "标注", "使用游标 A 的位置添加标注"),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda checked=False, selected=tool: self._set_interaction_tool(selected)
            )
            self.tool_button_group.addButton(button)
            self.tool_buttons[tool] = button
        self.tool_buttons[InteractionTool.ZOOM].setChecked(True)
        self.more_button = QToolButton(self)
        self.more_button.setText("更多")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        for text, callback in (
            ("导出当前视图区间", self._export_current_view_bundle),
            ("导出游标 A-B 区间", self._export_cursor_ab_bundle),
            ("导出相位诊断", self._export_phase_diagnostics),
        ):
            action = QAction(text, self.more_menu)
            action.triggered.connect(callback)
            self.more_menu.addAction(action)
        self.more_button.setMenu(self.more_menu)
        for button in (
            self.refresh_waveform_button,
            self.reset_waveform_button,
            self.undo_view_button,
            self.redo_view_button,
            self.capture_current_view_button,
            self.sidebar_toggle_button,
        ):
            action_row.addWidget(button)
        action_row.addSpacing(8)
        for tool in InteractionTool:
            action_row.addWidget(self.tool_buttons[tool])
        action_row.addWidget(self.more_button)
        action_row.addStretch(1)

        self.phase_channel_combo = QComboBox()
        self.phase_channel_combo.addItem("关闭", "")
        self.phase_channel_combo.currentIndexChanged.connect(self._sync_phase_compare_controls_to_panel)
        self.phase_mode_combo = QComboBox()
        self.phase_mode_combo.addItem("通用", "general")
        self.phase_mode_combo.addItem("编码器AB", "encoder_ab")
        self.phase_mode_combo.currentIndexChanged.connect(self._sync_phase_compare_controls_to_panel)
        self.phase_interval_combo = QComboBox()
        self.phase_interval_combo.addItem("自动", None)
        self.phase_interval_combo.addItem("10 us", 10e-6)
        self.phase_interval_combo.addItem("20 us", 20e-6)
        self.phase_interval_combo.addItem("50 us", 50e-6)
        self.phase_interval_combo.addItem("100 us", 100e-6)
        self.phase_interval_combo.currentIndexChanged.connect(self._sync_phase_compare_controls_to_panel)
        self.export_phase_diagnostics_button = QPushButton("导出相位差诊断")
        self.export_phase_diagnostics_button.clicked.connect(self._export_phase_diagnostics)
        self.phase_edge_combo = QComboBox()
        self.phase_edge_combo.addItem("上升沿", "rising")
        self.phase_edge_combo.addItem("下降沿", "falling")
        self.phase_edge_combo.currentIndexChanged.connect(self._sync_phase_compare_controls_to_panel)
        toolbar.addLayout(action_row)
        layout.addWidget(toolbar_container)
        set_uniform_control_height(toolbar_container)
        set_equal_button_widths(
            self.refresh_waveform_button,
            self.reset_waveform_button,
        )
        set_equal_button_widths(
            self.export_current_view_button,
            self.export_cursor_ab_button,
            self.capture_current_view_button,
        )

        self._default_operation_hint_text = (
            "左键框选放大时间轴，Shift+左键拖动平移，滚轮双轴缩放，Shift+滚轮缩放时间轴，Ctrl+滚轮缩放幅值，右键管理游标。"
        )
        self.operation_hint_label = QLabel(self._default_operation_hint_text)
        self.operation_hint_label.setWordWrap(True)
        self.operation_hint_label.setStyleSheet(f"color: {self.theme.secondary_text};")
        hint_font = self.operation_hint_label.font()
        hint_font.setPointSize(max(hint_font.pointSize() - 1, 9))
        self.operation_hint_label.setFont(hint_font)
        layout.addWidget(self.operation_hint_label)

        self.analysis_panel = WaveformAnalysisPanel(self, compact_mode=False)
        self.analysis_panel.set_theme(self.theme)
        self.analysis_panel.channel_unit_resolver = self._channel_unit
        self.analysis_panel.cursor_readout_changed = self._handle_cursor_measurements_changed
        self.analysis_panel.view_window_changed = self._on_view_window_changed
        self.analysis_panel.channel_comparison_changed = self._refresh_phase_compare_toolbar
        self.analysis_panel.annotation_requested = self._request_annotation_at_point
        self.analysis_panel.chart_view.selection_span_callback = self._show_selection_span
        self.analysis_panel.set_waveform_only_mode(True)
        self.workspace_row = QWidget(content)
        workspace_layout = QHBoxLayout(self.workspace_row)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(8)
        workspace_layout.addWidget(self.analysis_panel, 1)
        self.advanced_controls_widget = QGroupBox("分析侧栏", self.workspace_row)
        advanced_layout = QVBoxLayout(self.advanced_controls_widget)
        self.sidebar_toolbox = QToolBox(self.advanced_controls_widget)

        measurement_page = QWidget(self.sidebar_toolbox)
        measurement_layout = QFormLayout(measurement_page)
        measurement_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        measurement_layout.addRow("范围", self.measurement_scope_combo)
        measurement_layout.addRow(self.freeze_measurements_button)
        measurement_layout.addRow(self.measurement_settings_button)
        for button in (
            self.freeze_measurements_button,
            self.measurement_settings_button,
        ):
            button.setMinimumWidth(0)
            button.setMaximumWidth(16_777_215)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.measurement_detail_label = QLabel("尚未加载波形")
        self.measurement_detail_label.setWordWrap(True)
        self.measurement_detail_label.setTextFormat(Qt.RichText)
        self.measurement_detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        measurement_layout.addRow(self.measurement_detail_label)
        self.sidebar_toolbox.addItem(measurement_page, "测量")

        phase_page = QWidget(self.sidebar_toolbox)
        phase_layout = QFormLayout(phase_page)
        phase_layout.addRow("对比通道", self.phase_channel_combo)
        phase_layout.addRow("模式", self.phase_mode_combo)
        phase_layout.addRow("边沿", self.phase_edge_combo)
        phase_layout.addRow("最小间隔", self.phase_interval_combo)
        phase_layout.addRow(self.export_phase_diagnostics_button)
        self.sidebar_toolbox.addItem(phase_page, "相位")

        self.event_list = QListWidget(self.advanced_controls_widget)
        self.event_list.setToolTip("双击事件定位到对应时间。")
        self.event_list.itemClicked.connect(self._preview_selected_event)
        self.event_list.itemDoubleClicked.connect(self._focus_selected_event)
        event_page = QWidget(self.sidebar_toolbox)
        event_layout = QVBoxLayout(event_page)
        event_layout.addWidget(self.event_list)
        self.sidebar_toolbox.addItem(event_page, "事件")

        self.add_annotation_button = QPushButton("在游标 A 添加标注")
        self.add_annotation_button.clicked.connect(self._add_annotation_at_cursor)
        annotation_page = QWidget(self.sidebar_toolbox)
        annotation_layout = QVBoxLayout(annotation_page)
        self.cursor_detail_label = QLabel("尚未放置游标")
        self.cursor_detail_label.setWordWrap(True)
        self.cursor_detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        annotation_layout.addWidget(self.cursor_detail_label)
        annotation_layout.addWidget(self.add_annotation_button)
        annotation_layout.addStretch(1)
        self.sidebar_toolbox.addItem(annotation_page, "游标与标注")

        self.bookmark_combo = QComboBox(self.advanced_controls_widget)
        self.bookmark_combo.addItem("视图书签")
        self.bookmark_combo.activated.connect(self._restore_selected_bookmark)
        self.add_bookmark_button = QPushButton("保存当前视图书签")
        self.add_bookmark_button.clicked.connect(self._add_view_bookmark)
        self.load_reference_button = QPushButton("加载参考波形")
        self.load_reference_button.clicked.connect(self._load_reference_waveforms)
        self.reference_result_label = QLabel("参考对比：未加载")
        self.reference_result_label.setWordWrap(True)
        reference_page = QWidget(self.sidebar_toolbox)
        reference_layout = QVBoxLayout(reference_page)
        reference_layout.addWidget(self.load_reference_button)
        reference_layout.addWidget(self.reference_result_label)
        reference_layout.addWidget(self.bookmark_combo)
        reference_layout.addWidget(self.add_bookmark_button)
        reference_layout.addStretch(1)
        self.sidebar_toolbox.addItem(reference_page, "参考与书签")

        display_page = QWidget(self.sidebar_toolbox)
        display_layout = QVBoxLayout(display_page)
        self.measurement_overlay_check = QCheckBox("显示核心测量浮层")
        self.measurement_overlay_check.setChecked(True)
        self.cursor_overlay_check = QCheckBox("显示游标浮层")
        self.cursor_overlay_check.setChecked(True)
        display_layout.addWidget(self.measurement_overlay_check)
        display_layout.addWidget(self.cursor_overlay_check)
        display_layout.addStretch(1)
        self.sidebar_toolbox.addItem(display_page, "显示设置")
        self.sidebar_toolbox.currentChanged.connect(self._sidebar_section_changed)
        advanced_layout.addWidget(self.sidebar_toolbox, 1)
        self.advanced_controls_widget.setFixedWidth(WAVEFORM_SIDEBAR_WIDTH)
        workspace_layout.addWidget(self.advanced_controls_widget)
        layout.addWidget(self.workspace_row, 1)
        overview_row = QHBoxLayout()
        overview_row.addWidget(QLabel("全局导航"))
        self.overview_navigator = WaveformNavigator(content, theme=self.theme)
        self.overview_navigator.setToolTip("拖动高亮区域平移；拖动左右手柄调整时间窗口。")
        self.overview_navigator.rangeChanged.connect(self._overview_range_changed)
        overview_row.addWidget(self.overview_navigator, 1)
        layout.addLayout(overview_row)

        self.channel_toggle_container = QWidget(self.analysis_panel)
        self.channel_toggle_layout = QHBoxLayout(self.channel_toggle_container)
        self.channel_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_toggle_layout.setSpacing(8)
        channel_page = QWidget(self.sidebar_toolbox)
        channel_layout = QVBoxLayout(channel_page)
        channel_layout.addWidget(self.channel_toggle_container)
        channel_layout.addStretch(1)
        self.sidebar_toolbox.insertItem(0, channel_page, "通道")
        for tab_button in self.sidebar_toolbox.findChildren(
            QAbstractButton,
            "qt_toolbox_toolboxbutton",
        ):
            tab_button.setMinimumHeight(WAVEFORM_SIDEBAR_TAB_HEIGHT)
        for sidebar_button in (
            self.freeze_measurements_button,
            self.measurement_settings_button,
            self.export_phase_diagnostics_button,
            self.add_annotation_button,
            self.load_reference_button,
            self.add_bookmark_button,
        ):
            sidebar_button.setMinimumWidth(0)
            sidebar_button.setMaximumWidth(16_777_215)
            sidebar_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.measurement_overlay = QFrame(self.analysis_panel.chart_view)
        self.measurement_overlay.setObjectName("measurementOverlay")
        self.measurement_overlay.setFrameShape(QFrame.StyledPanel)
        self.measurement_overlay.setStyleSheet(
            f"#measurementOverlay {{ background-color: {self.theme.overlay_background}; "
            f"border: 1px solid {self.theme.border}; border-radius: 8px; }}"
        )
        overlay_layout = QVBoxLayout(self.measurement_overlay)
        overlay_layout.setContentsMargins(6, 4, 6, 4)
        overlay_layout.setSpacing(3)
        measurement_header = QHBoxLayout()
        measurement_header.setContentsMargins(0, 0, 0, 0)
        self.measurement_overlay_hint = QLabel("波形测量数据会显示在这里。")
        self.measurement_overlay_hint.setWordWrap(True)
        hint_font = self.measurement_overlay_hint.font()
        hint_font.setPointSize(OVERLAY_TITLE_POINT_SIZE)
        self.measurement_overlay_hint.setFont(hint_font)
        self.measurement_overlay_hint.setStyleSheet(f"color: {self.theme.secondary_text};")
        measurement_header.addWidget(self.measurement_overlay_hint, 1)
        self.measurement_overlay_collapse_button = QToolButton(self.measurement_overlay)
        self.measurement_overlay_collapse_button.setText("−")
        self.measurement_overlay_collapse_button.setToolTip("折叠当前测量范围")
        self.measurement_overlay_collapse_button.clicked.connect(
            lambda: self._set_overlay_collapsed(
                "measurement",
                not self.measurement_text_label.isHidden(),
            )
        )
        measurement_header.addWidget(self.measurement_overlay_collapse_button)
        overlay_layout.addLayout(measurement_header)

        self.measurement_text_label = QLabel(self.measurement_overlay)
        self.measurement_text_label.setWordWrap(True)
        self.measurement_text_label.setTextFormat(Qt.RichText)
        self.measurement_text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_font = self.measurement_text_label.font()
        text_font.setPointSize(OVERLAY_BODY_POINT_SIZE)
        self.measurement_text_label.setFont(text_font)
        overlay_layout.addWidget(self.measurement_text_label)
        # The overlay geometry is managed manually. A minimum-size layout
        # constraint would keep the expanded height after its content is hidden.
        overlay_layout.setSizeConstraint(QVBoxLayout.SetNoConstraint)
        self.measurement_overlay.hide()

        self.cursor_overlay = QFrame(self.analysis_panel.chart_view)
        self.cursor_overlay.setObjectName("cursorOverlay")
        self.cursor_overlay.setFrameShape(QFrame.StyledPanel)
        self.cursor_overlay.setStyleSheet(
            f"#cursorOverlay {{ background-color: {self.theme.overlay_background}; "
            f"border: 1px solid {self.theme.border}; border-radius: 8px; }}"
        )
        cursor_layout = QVBoxLayout(self.cursor_overlay)
        cursor_layout.setContentsMargins(4, 3, 4, 3)
        cursor_layout.setSpacing(3)
        cursor_header = QHBoxLayout()
        cursor_header.setContentsMargins(0, 0, 0, 0)
        self.cursor_overlay_hint = QLabel("■ 游标测量")
        cursor_hint_font = self.cursor_overlay_hint.font()
        cursor_hint_font.setPointSize(OVERLAY_TITLE_POINT_SIZE)
        cursor_hint_font.setBold(True)
        self.cursor_overlay_hint.setFont(cursor_hint_font)
        self.cursor_overlay_hint.setStyleSheet(
            f"color: {self.theme.axis_text}; letter-spacing: 0.4px;"
        )
        cursor_header.addWidget(self.cursor_overlay_hint, 1)
        self.cursor_overlay_collapse_button = QToolButton(self.cursor_overlay)
        self.cursor_overlay_collapse_button.setText("−")
        self.cursor_overlay_collapse_button.setToolTip("折叠游标测量")
        self.cursor_overlay_collapse_button.clicked.connect(
            lambda: self._set_overlay_collapsed(
                "cursor",
                not self.cursor_text_label.isHidden(),
            )
        )
        cursor_header.addWidget(self.cursor_overlay_collapse_button)
        cursor_layout.addLayout(cursor_header)
        self.cursor_text_label = QLabel(self.cursor_overlay)
        self.cursor_text_label.setWordWrap(True)
        self.cursor_text_label.setTextFormat(Qt.RichText)
        self.cursor_text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cursor_text_label.setFont(text_font)
        cursor_layout.addWidget(self.cursor_text_label)
        cursor_layout.setSizeConstraint(QVBoxLayout.SetNoConstraint)
        self.cursor_overlay.hide()
        self.measurement_overlay_check.toggled.connect(self._refresh_measurement_footer)
        self.cursor_overlay_check.toggled.connect(self._refresh_measurement_footer)
        self.tool_capsule = QLabel("缩放", self.analysis_panel.chart_view)
        self.tool_capsule.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.tool_capsule.setStyleSheet(
            f"background: {self.theme.overlay_background}; color: {self.theme.primary_text}; "
            f"border: 1px solid {self.theme.border}; border-radius: 9px; padding: 3px 9px;"
        )
        self.tool_capsule.adjustSize()
        self.tool_capsule.move(18, 18)
        self.tool_capsule.raise_()
        self.analysis_panel.chart_view.installEventFilter(self)
        self.phase_result_label = QLabel("相位差: --")
        self.phase_result_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.phase_result_label)
        self.phase_metrics_label = QLabel("")
        self.phase_metrics_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.phase_metrics_label.setStyleSheet(f"color: {self.theme.secondary_text};")
        layout.addWidget(self.phase_metrics_label)
        self.workspace_status_label = QLabel("模式：离线/单次　尚未加载波形")
        self.workspace_status_label.setStyleSheet(
            f"color: {self.theme.secondary_text}; padding: 3px 6px;"
        )
        self.quality_badge_label = QLabel("正常")
        self.quality_badge_label.setAlignment(Qt.AlignCenter)
        status_row = QHBoxLayout()
        status_row.addWidget(self.workspace_status_label, 1)
        status_row.addWidget(self.quality_badge_label)
        layout.addLayout(status_row)
        self._install_workspace_shortcuts()
        self._restore_workspace_settings()
        self._set_workspace_actions_enabled(False)
        self.undo_view_button.setEnabled(False)
        self.redo_view_button.setEnabled(False)

    def _set_workspace_actions_enabled(self, enabled: bool) -> None:
        for widget in (
            self.reset_waveform_button,
            self.capture_current_view_button,
            self.more_button,
            self.add_annotation_button,
            self.add_bookmark_button,
            self.load_reference_button,
        ):
            widget.setEnabled(enabled)
        for tool, button in self.tool_buttons.items():
            button.setEnabled(enabled or tool is InteractionTool.ZOOM)
        reason = "" if enabled else "请先抓取或加载波形。"
        for widget in (
            self.reset_waveform_button,
            self.capture_current_view_button,
            self.more_button,
        ):
            if not enabled:
                widget.setToolTip(reason)

    def set_loading(self, message: str) -> None:
        self.operation_hint_label.setText(message)
        self.refresh_waveform_button.setText("抓取中...")
        self.refresh_waveform_button.setEnabled(False)

    def clear_loading(self) -> None:
        self.operation_hint_label.setText(self._default_operation_hint_text)
        self.refresh_waveform_button.setText("抓取")
        self.refresh_waveform_button.setEnabled(True)

    def set_loading_failed(self, message: str) -> None:
        self.operation_hint_label.setText(message)
        self.refresh_waveform_button.setText("抓取")
        self.refresh_waveform_button.setEnabled(True)

    def _install_workspace_shortcuts(self) -> None:
        shortcuts = (
            ("F", self.analysis_panel.reset_view),
            ("Ctrl+Z", self._undo_view),
            ("Ctrl+Y", self._redo_view),
            ("A", lambda: self._set_interaction_tool(InteractionTool.CURSOR_A)),
            ("B", lambda: self._set_interaction_tool(InteractionTool.CURSOR_B)),
            ("Escape", lambda: self._set_interaction_tool(InteractionTool.ZOOM)),
            ("Left", lambda: self.analysis_panel.pan_horizontal(-0.1)),
            ("Right", lambda: self.analysis_panel.pan_horizontal(0.1)),
        )
        self._workspace_shortcuts: list[QShortcut] = []
        for sequence, callback in shortcuts:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._workspace_shortcuts.append(shortcut)

    def _set_interaction_tool(self, tool: InteractionTool) -> None:
        self.interaction_tool = tool
        self.analysis_panel.set_interaction_tool(tool)
        for candidate, button in self.tool_buttons.items():
            button.setChecked(candidate is tool)
        labels = {
            InteractionTool.ZOOM: "缩放",
            InteractionTool.PAN: "平移",
            InteractionTool.CURSOR_A: "游标 A",
            InteractionTool.CURSOR_B: "游标 B",
            InteractionTool.ANNOTATE: "标注",
        }
        self.tool_capsule.setText(labels[tool])
        self.tool_capsule.adjustSize()
        self._show_status_message(f"当前工具：{labels[tool]}")

    def _show_selection_span(self, span_s: float) -> None:
        self._show_status_message(f"框选跨度：{format_engineering_value(span_s, 's')}")

    def _show_status_message(self, message: str, duration_ms: int = 1800) -> None:
        self.operation_hint_label.setText(message)
        QTimer.singleShot(
            duration_ms,
            lambda: self.operation_hint_label.setText(self._default_operation_hint_text),
        )

    def _sidebar_section_changed(self, index: int) -> None:
        if index >= 0:
            self._settings.setValue("sidebar_section", self.sidebar_toolbox.itemText(index))

    def _preview_selected_event(self, item) -> None:
        index = self.event_list.row(item)
        if index < 0 or index >= len(getattr(self, "_quick_events", ())):
            return
        event = self._quick_events[index]
        self._show_status_message(
            f"{display_channel_name(event.channel)} · {event.description} · "
            f"{format_engineering_value(event.time_s, 's')}"
        )

    def _toggle_sidebar(self, hidden: bool) -> None:
        self.advanced_controls_widget.setVisible(not hidden)
        self.sidebar_toggle_button.setText("显示侧栏" if hidden else "隐藏侧栏")

    def _set_overlay_collapsed(self, overlay: str, collapsed: bool) -> None:
        if overlay == "measurement":
            content = self.measurement_text_label
            button = self.measurement_overlay_collapse_button
            title = "展开当前测量范围" if collapsed else "折叠当前测量范围"
        else:
            content = self.cursor_text_label
            button = self.cursor_overlay_collapse_button
            title = "展开游标测量" if collapsed else "折叠游标测量"
        content.setVisible(not collapsed)
        button.setText("+" if collapsed else "−")
        button.setToolTip(title)
        self._reposition_measurement_overlay()

    def _restore_workspace_settings(self) -> None:
        geometry = self._settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.sidebar_toggle_button.setChecked(
            self._settings.value("sidebar_hidden", False, type=bool)
        )
        self.measurement_overlay_check.setChecked(
            self._settings.value("measurement_overlay_visible", True, type=bool)
        )
        self.cursor_overlay_check.setChecked(
            self._settings.value("cursor_overlay_visible", True, type=bool)
        )
        self._set_overlay_collapsed(
            "measurement",
            self._settings.value("measurement_overlay_collapsed", False, type=bool),
        )
        self._set_overlay_collapsed(
            "cursor",
            self._settings.value("cursor_overlay_collapsed", False, type=bool),
        )
        saved_section = str(self._settings.value("sidebar_section", "测量"))
        for index in range(self.sidebar_toolbox.count()):
            if self.sidebar_toolbox.itemText(index) == saved_section:
                self.sidebar_toolbox.setCurrentIndex(index)
                break
        try:
            saved_tool = InteractionTool(str(self._settings.value("active_tool", InteractionTool.ZOOM.value)))
        except ValueError:
            saved_tool = InteractionTool.ZOOM
        self._set_interaction_tool(saved_tool)

    def _save_workspace_settings(self) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("sidebar_hidden", self.sidebar_toggle_button.isChecked())
        self._settings.setValue("active_tool", self.interaction_tool.value)
        self._settings.setValue(
            "measurement_overlay_visible",
            self.measurement_overlay_check.isChecked(),
        )
        self._settings.setValue(
            "cursor_overlay_visible",
            self.cursor_overlay_check.isChecked(),
        )
        self._settings.setValue(
            "measurement_overlay_collapsed",
            self.measurement_text_label.isHidden(),
        )
        self._settings.setValue(
            "cursor_overlay_collapsed",
            self.cursor_text_label.isHidden(),
        )
        self._settings.setValue(
            "visible_channels",
            sorted(
                channel
                for channel, checkbox in self.channel_visibility_checks.items()
                if checkbox.isChecked()
            ),
        )

    def _on_view_window_changed(self) -> None:
        self._refresh_measurement_footer()
        if not self._restoring_view:
            self._view_history_timer.start()
        self._update_workspace_status()
        self._sync_overview_position()

    def _sync_overview_position(self) -> None:
        state = self.analysis_panel.capture_view_state()
        if not state or not self.current_waveforms:
            return
        populated = [waveform for waveform in self.current_waveforms if waveform.x_values]
        if not populated:
            return
        full_left = min(waveform.x_values[0] for waveform in populated)
        full_right = max(waveform.x_values[-1] for waveform in populated)
        x_range = state.get("x_range")
        if not isinstance(x_range, tuple) or full_right <= full_left:
            return
        self.overview_navigator.set_ranges(
            (full_left, full_right),
            (float(x_range[0]), float(x_range[1])),
        )

    def _overview_range_changed(self, left: float, right: float) -> None:
        axis = self.analysis_panel._x_axis()
        if axis is not None:
            axis.setRange(left, right)

    def _current_typed_view_state(self) -> WaveformViewState | None:
        state = self.analysis_panel.capture_view_state()
        if not state:
            return None
        x_range = state.get("x_range")
        y_range = state.get("y_range")
        if not isinstance(x_range, tuple) or not isinstance(y_range, tuple):
            return None
        return WaveformViewState(
            x_range=(float(x_range[0]), float(x_range[1])),
            y_range=(float(y_range[0]), float(y_range[1])),
            visible_channels=tuple(sorted(self.analysis_panel.visible_channels)),
            waveform_offsets=dict(self.analysis_panel.waveform_offsets),
            cursor_points=dict(self.analysis_panel.cursor_points),
            active_tool=self.interaction_tool.value,
            sidebar_section=self.sidebar_toolbox.itemText(self.sidebar_toolbox.currentIndex()),
            measurement_overlay_visible=self.measurement_overlay_check.isChecked(),
            cursor_overlay_visible=self.cursor_overlay_check.isChecked(),
            theme_name=self.theme.name,
        )

    def _record_current_view(self) -> None:
        state = self._current_typed_view_state()
        if state is not None:
            self._view_history.push(state)
        self.undo_view_button.setEnabled(self._view_history.can_undo)
        self.redo_view_button.setEnabled(self._view_history.can_redo)

    def _restore_typed_view_state(self, state: WaveformViewState | None) -> None:
        if state is None:
            return
        self._restoring_view = True
        try:
            self.analysis_panel.restore_view_state(
                {
                    "visible_channels": set(state.visible_channels),
                    "waveform_offsets": state.waveform_offsets,
                    "x_range": state.x_range,
                    "y_range": state.y_range,
                }
            )
            self.analysis_panel.cursor_points = dict(state.cursor_points)
            self.analysis_panel._refresh_cursor_graphics()
            try:
                self._set_interaction_tool(InteractionTool(state.active_tool))
            except ValueError:
                self._set_interaction_tool(InteractionTool.ZOOM)
            self.measurement_overlay_check.setChecked(state.measurement_overlay_visible)
            self.cursor_overlay_check.setChecked(state.cursor_overlay_visible)
        finally:
            self._restoring_view = False
        self.undo_view_button.setEnabled(self._view_history.can_undo)
        self.redo_view_button.setEnabled(self._view_history.can_redo)
        self._refresh_measurement_footer()

    def _undo_view(self) -> None:
        self._restore_typed_view_state(self._view_history.undo())

    def _redo_view(self) -> None:
        self._restore_typed_view_state(self._view_history.redo())

    def _update_workspace_status(self) -> None:
        raw_points = sum(len(waveform.x_values) for waveform in self.current_waveforms)
        displayed_points = self.analysis_panel.displayed_point_count()
        view_state = self.analysis_panel.capture_view_state()
        range_text = "--"
        if view_state and isinstance(view_state.get("x_range"), tuple):
            left, right = view_state["x_range"]
            range_text = f"{float(left):.4g}…{float(right):.4g} s"
        issues = inspect_waveforms(self.current_waveforms)
        issue_text = "正常" if not issues else f"{len(issues)} 项警告"
        badge_color = self.theme.success
        if any(issue.severity == "error" for issue in issues):
            badge_color = self.theme.error
        elif issues:
            badge_color = self.theme.warning
        self.quality_badge_label.setText(issue_text)
        self.quality_badge_label.setStyleSheet(
            f"color: {badge_color}; border: 1px solid {badge_color}; "
            "border-radius: 8px; padding: 2px 8px;"
        )
        self.workspace_status_label.setText(
            f"模式：离线/单次　原始点：{raw_points:,}　显示点：{displayed_points:,}　"
            f"范围：{range_text}"
        )
        issue_details = "\n".join(issue.message for issue in issues)
        self.workspace_status_label.setToolTip(issue_details)
        self.quality_badge_label.setToolTip(issue_details)

    def set_waveform(self, waveform: WaveformData, stats: WaveformStats) -> None:
        self.current_waveforms = [waveform]
        self.analysis_panel.set_waveform(waveform, stats)
        self._ensure_measurement_defaults([waveform])
        self._sync_phase_compare_toolbar_from_panel()
        self._refresh_measurement_footer()
        self._rebuild_channel_visibility_checks([waveform])
        self._frame_received()

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        self.current_waveforms = list(waveforms)
        self.analysis_panel.set_waveforms(waveforms, primary_stats)
        self._ensure_measurement_defaults(waveforms)
        self._sync_phase_compare_toolbar_from_panel()
        self._refresh_measurement_footer()
        self._rebuild_channel_visibility_checks(waveforms)
        self._frame_received()

    def _frame_received(self) -> None:
        self._set_workspace_actions_enabled(bool(self.current_waveforms))
        self._refresh_event_list()
        self._record_current_view()
        self._update_workspace_status()

    def _refresh_event_list(self) -> None:
        self._quick_events = detect_quick_events(self.current_waveforms)
        self.event_list.clear()
        for event in self._quick_events[:500]:
            symbol = {"rising": "↗", "falling": "↘", "maximum": "▲", "minimum": "▼"}.get(
                event.event_type,
                "•",
            )
            item_text = (
                f"{symbol} {display_channel_name(event.channel)}  {event.description}  "
                f"{event.time_s:.6g}s  {event.value:.6g}"
            )
            self.event_list.addItem(item_text)
            item = self.event_list.item(self.event_list.count() - 1)
            item.setForeground(QBrush(QColor(self.theme.channel_style(event.channel).color)))

    def _focus_selected_event(self, item) -> None:
        index = self.event_list.row(item)
        if index < 0 or index >= len(getattr(self, "_quick_events", ())):
            return
        event = self._quick_events[index]
        self.analysis_panel.focus_on_channel_point(
            (event.time_s, event.value),
            channel=event.channel,
            annotation_text=event.description,
        )

    def _add_annotation_at_cursor(self) -> None:
        point = self.analysis_panel.cursor_points.get("a")
        if point is None:
            self._log_message("请先放置游标 A。")
            return
        self._request_annotation_at_point(point, self.analysis_panel.cursor_channels.get("a"))

    def _request_annotation_at_point(
        self,
        point: tuple[float, float],
        channel: str | None,
    ) -> None:
        text, accepted = QInputDialog.getText(self, "添加标注", "标注内容")
        if not accepted or not text.strip():
            return
        annotation = WaveformAnnotation(
            annotation_id=f"annotation-{len(self._annotations) + 1}",
            text=text.strip(),
            start_time_s=point[0],
        )
        self._annotations.append(annotation)
        self.analysis_panel.focus_on_channel_point(
            point,
            channel=channel,
            annotation_text=annotation.text,
        )
        self._show_status_message("标注已添加")
        self._set_interaction_tool(InteractionTool.ZOOM)

    def _add_view_bookmark(self) -> None:
        state = self._current_typed_view_state()
        if state is None:
            self._log_message("当前没有可保存的视图。")
            return
        name, accepted = QInputDialog.getText(self, "保存视图书签", "书签名称")
        if not accepted or not name.strip():
            return
        name = name.strip()
        self._bookmarks[name] = state
        if self.bookmark_combo.findText(name) < 0:
            self.bookmark_combo.addItem(name)
        self.bookmark_combo.setCurrentText(name)

    def _restore_selected_bookmark(self, index: int) -> None:
        if index <= 0:
            return
        self._restore_typed_view_state(
            self._bookmarks.get(self.bookmark_combo.itemText(index))
        )

    def _load_reference_waveforms(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "加载参考波形",
            str(WAVEFORM_CONFIG_DIR),
            "CSV Files (*.csv)",
        )
        if not file_paths:
            return
        reference_waveforms: list[WaveformData] = []
        try:
            for file_path in file_paths:
                reference_waveforms.extend(WaveformData.load_csv_bundle(Path(file_path)))
        except (OSError, ValueError) as exc:
            self._log_message(f"参考波形加载失败: {exc}")
            return
        self.analysis_panel.set_reference_waveforms(reference_waveforms)
        current_by_channel = {waveform.channel: waveform for waveform in self.current_waveforms}
        metrics: list[str] = []
        for reference in reference_waveforms:
            candidate = current_by_channel.get(reference.channel)
            if candidate is None:
                continue
            try:
                comparison = compare_to_baseline(reference, candidate)
            except ValueError:
                continue
            metrics.append(
                f"{display_channel_name(reference.channel)} RMS={comparison.rms_error:.4g}, "
                f"Max={comparison.maximum_absolute_error:.4g}"
            )
        self.reference_result_label.setText(
            "参考对比：" + ("；".join(metrics) if metrics else "没有同通道重叠数据")
        )

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
        self._measurement_frozen = False
        self.freeze_measurements_button.blockSignals(True)
        self.freeze_measurements_button.setChecked(False)
        self.freeze_measurements_button.setText("冻结测量")
        self.freeze_measurements_button.blockSignals(False)
        self.analysis_panel.clear()
        self._sync_phase_compare_toolbar_from_panel()
        self._refresh_measurement_footer()
        self._rebuild_channel_visibility_checks([])
        self._set_workspace_actions_enabled(False)
        self._update_workspace_status()

    def set_cursor_points(
        self,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        *,
        annotation_text: str | None = None,
    ) -> None:
        self.analysis_panel.set_cursor_points(point_a, point_b, annotation_text=annotation_text)

    def export_standardized_snapshot(
        self,
        output_path: Path,
        *,
        visible_channels: list[str],
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        annotation_text: str,
        padding_ratio: float = 0.15,
    ) -> bool:
        if not self.current_waveforms:
            return False

        output_path.parent.mkdir(parents=True, exist_ok=True)
        saved_view_state = self.analysis_panel.capture_view_state()
        saved_cursor_points = dict(self.analysis_panel.cursor_points)
        saved_cursor_channels = dict(self.analysis_panel.cursor_channels)
        saved_annotation = self.analysis_panel.lock_annotation_text
        measurement_visible = self.measurement_overlay.isVisible()
        cursor_overlay_visible = self.cursor_overlay.isVisible()

        try:
            self.analysis_panel.set_visible_channels(set(visible_channels))
            self.analysis_panel.reset_view()
            self.analysis_panel.stack_visible_channels_for_export()
            self.analysis_panel.set_cursor_points(point_a, point_b, annotation_text=annotation_text)
            self.analysis_panel.frame_time_window(point_a[0], point_b[0], padding_ratio=padding_ratio)
            self._refresh_measurement_footer()
            self.measurement_overlay.hide()
            if self.cursor_measurements:
                self.cursor_overlay_hint.setText("游标测量")
                self.cursor_text_label.setText(self._build_cursor_measurement_section_html())
                self.cursor_overlay.show()
                self._reposition_measurement_overlay()
            else:
                self.cursor_overlay.hide()
            self.analysis_panel.chart_view.repaint()
            QApplication.processEvents()
            QApplication.processEvents()
            image = self.analysis_panel.render_chart_image()
            self._decorate_export_image(image)
            return image.save(str(output_path), "PNG")
        finally:
            self.analysis_panel.restore_view_state(saved_view_state)
            self.analysis_panel.cursor_points = saved_cursor_points
            self.analysis_panel.cursor_channels = saved_cursor_channels
            self.analysis_panel.lock_annotation_text = saved_annotation
            self.analysis_panel._refresh_cursor_graphics()
            self._refresh_measurement_footer()
            if measurement_visible:
                self.measurement_overlay.show()
            if cursor_overlay_visible:
                self.cursor_overlay.show()

    def _rebuild_channel_visibility_checks(self, waveforms: list[WaveformData]) -> None:
        previous_states = {
            channel: checkbox.isChecked()
            for channel, checkbox in self.channel_visibility_checks.items()
        }
        saved_visible = self._settings.value("visible_channels", [])
        if isinstance(saved_visible, str):
            saved_visible = [saved_visible]
        while self.channel_toggle_layout.count():
            item = self.channel_toggle_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.channel_toggle_layout.addStretch(1)
        self.channel_toggle_layout.addWidget(QLabel("显示通道"))
        self.link_scope_channels_check = QCheckBox("联动示波器通道")
        self.link_scope_channels_check.setChecked(self._link_scope_channels)
        self.link_scope_channels_check.setToolTip("勾选后，这里的通道开关会同时控制示波器通道显示；不勾选时只影响当前独立波形窗口。")
        self.link_scope_channels_check.toggled.connect(self._set_link_scope_channels_enabled)
        self.channel_toggle_layout.addWidget(self.link_scope_channels_check)
        self.channel_visibility_checks = {}
        active_waveform_channels = {waveform.channel for waveform in waveforms}
        for channel in SUPPORTED_CHANNELS:
            checkbox = QCheckBox(display_channel_name(channel))
            checkbox.setStyleSheet(
                f"color: {self.theme.channel_style(channel).color}; font-weight: 600;"
            )
            default_checked = (
                channel in active_waveform_channels
                and (not saved_visible or channel in saved_visible)
            )
            checked = previous_states.get(channel, default_checked)
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
        self._sync_phase_compare_toolbar_from_panel()
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
        if self._link_scope_channels:
            parent = self.parent()
            if parent is not None and hasattr(parent, "request_scope_channel_display_from_detail_dialog"):
                parent.request_scope_channel_display_from_detail_dialog(channel, checked)
                return
        self._apply_channel_visibility()

    def sync_scope_channel_checks(self, channels: list[str]) -> None:
        active_channels = set(channels)
        if self._link_scope_channels:
            self._updating_channel_checks = True
            try:
                for channel, checkbox in self.channel_visibility_checks.items():
                    checkbox.setEnabled(True)
                    checkbox.setChecked(channel in active_channels)
            finally:
                self._updating_channel_checks = False
        else:
            for channel, checkbox in self.channel_visibility_checks.items():
                checkbox.setEnabled(True)
                if channel in active_channels and channel not in self.analysis_panel.visible_channels:
                    checkbox.setChecked(True)
        self._apply_channel_visibility()

    def _set_link_scope_channels_enabled(self, enabled: bool) -> None:
        self._link_scope_channels = enabled
        if not enabled:
            return
        parent = self.parent()
        if parent is not None and hasattr(parent, "scope_display_checks"):
            parent_checks = getattr(parent, "scope_display_checks", {})
            active_channels = [
                channel
                for channel, checkbox in parent_checks.items()
                if checkbox.isChecked()
            ]
            self.sync_scope_channel_checks(active_channels)

    def _reset_waveform_view(self) -> None:
        self.analysis_panel.reset_view()

    def _export_current_view_bundle(self) -> None:
        if not self.current_waveforms:
            self._log_message("当前没有可导出的波形。")
            return
        time_window = self.analysis_panel.current_time_window()
        if time_window is None:
            self._log_message("当前视图没有有效时间窗，无法导出。")
            return
        self._export_waveform_segment("view", time_window, write_markers=False)

    def _export_cursor_ab_bundle(self) -> None:
        if not self.current_waveforms:
            self._log_message("当前没有可导出的波形。")
            return
        time_window = self.analysis_panel.cursor_time_window()
        if time_window is None:
            self._log_message("请先放置 A/B 游标，再导出游标区间。")
            return
        self._export_waveform_segment("cursor_ab", time_window, write_markers=True)

    def _export_waveform_segment(
        self,
        segment_name: str,
        time_window: tuple[float, float],
        *,
        write_markers: bool,
    ) -> None:
        left_time, right_time = time_window
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = WAVEFORM_CONFIG_DIR / f"{segment_name}_{timestamp}.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出波形 CSV",
            str(default_path),
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        visible_channels = list(self.analysis_panel.visible_channels or {waveform.channel for waveform in self.current_waveforms})
        clipped_waveforms = [
            clipped
            for clipped in (
                self._clip_waveform_to_time_range(waveform, left_time, right_time)
                for waveform in self.current_waveforms
                if waveform.channel in visible_channels
            )
            if clipped is not None
        ]
        if not clipped_waveforms:
            self._log_message("选定时间范围内没有可导出的波形数据。")
            return

        output_path = Path(file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        WaveformData.export_csv_bundle(clipped_waveforms, output_path)
        if write_markers:
            point_a = self.analysis_panel.cursor_points.get("a")
            point_b = self.analysis_panel.cursor_points.get("b")
            if point_a is not None and point_b is not None:
                self._write_marker_sidecar(output_path, point_a, point_b, "Cursor Window")
        save_workspace_metadata(
            output_path.with_suffix(".workspace.json"),
            bookmarks=self._bookmarks,
            annotations=self._annotations,
        )
        self._log_message(f"波形局部已导出: {output_path}")

    def _capture_current_view_image(self) -> None:
        if not self.current_waveforms:
            self._log_message("当前没有可截图的波形。")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = WAVEFORM_CONFIG_DIR / f"view_{timestamp}.png"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前视图截图",
            str(default_path),
            "PNG Files (*.png)",
        )
        if not file_path:
            return
        output_path = Path(file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.analysis_panel.chart_view.repaint()
        self._reposition_measurement_overlay()
        QApplication.processEvents()
        QApplication.processEvents()
        image = self._render_current_view_image(scale=2.0)
        self._decorate_export_image(image)
        if image.save(str(output_path), "PNG"):
            self._log_message(f"当前视图截图已保存: {output_path}")
        else:
            self._log_message(f"当前视图截图保存失败: {output_path}")

    def _export_phase_diagnostics(self) -> None:
        target_channel, edge_type, comparison_mode, minimum_edge_interval_s, comparison, message = self.analysis_panel.channel_comparison_state()
        if not target_channel:
            self._log_message("请先选择相位差通道，再导出相位差诊断。")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        diagnostics_dir = WAVEFORM_CONFIG_DIR / "phase_diagnostics"
        default_path = diagnostics_dir / f"phase_diagnostics_{timestamp}.json"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出相位差诊断",
            str(default_path),
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        output_path = Path(file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_phase_diagnostics_payload(
            target_channel=str(target_channel),
            edge_type=edge_type,
            comparison_mode=comparison_mode,
            minimum_edge_interval_s=minimum_edge_interval_s,
            comparison=comparison,
            message=message,
        )
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log_message(f"相位差诊断已导出: {output_path}")

    def _build_phase_diagnostics_payload(
        self,
        *,
        target_channel: str,
        edge_type: str,
        comparison_mode: str,
        minimum_edge_interval_s: float | None,
        comparison,
        message: str | None,
    ) -> dict[str, object]:
        reference_channel = self.analysis_panel.active_waveform_channel
        current_window = self.analysis_panel.current_time_window()
        return {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "reference_channel": reference_channel,
            "target_channel": target_channel,
            "mode": comparison_mode,
            "edge_type": edge_type,
            "minimum_edge_interval_s": minimum_edge_interval_s,
            "time_window_s": list(current_window) if current_window is not None else None,
            "visible_channels": sorted(self.analysis_panel.visible_channels),
            "comparison": None
            if comparison is None
            else {
                "delta_t_s": comparison.delta_t_s,
                "phase_deg": comparison.phase_deg,
                "frequency_hz": comparison.frequency_hz,
                "confidence": comparison.confidence,
                "raw_transition_count": comparison.raw_transition_count,
                "debounce_filtered_count": comparison.debounce_filtered_count,
                "sequence_discarded_count": comparison.sequence_discarded_count,
                "valid_transition_count": comparison.valid_transition_count,
                "invalid_transition_count": comparison.invalid_transition_count,
                "primary_time_s": comparison.primary_time_s,
                "secondary_time_s": comparison.secondary_time_s,
            },
            "message": message,
            "reference_stats": self._phase_channel_stats_snapshot(reference_channel),
            "target_stats": self._phase_channel_stats_snapshot(target_channel),
        }

    def _phase_channel_stats_snapshot(self, channel: str | None) -> dict[str, object] | None:
        if not channel:
            return None
        visible_stats = self.analysis_panel.visible_stats_for_channel(channel)
        full_stats = self.analysis_panel.full_stats_for_channel(channel)
        if visible_stats is None and full_stats is None:
            return None
        return {
            "channel": channel,
            "visible": None
            if visible_stats is None
            else {
                "frequency_hz": visible_stats.estimated_frequency_hz,
                "pulse_count": visible_stats.pulse_count,
                "logic_low_v": visible_stats.logic_low_v,
                "logic_high_v": visible_stats.logic_high_v,
                "sample_period_s": visible_stats.sample_period_s,
                "duration_s": visible_stats.duration_s,
            },
            "full": None
            if full_stats is None
            else {
                "frequency_hz": full_stats.estimated_frequency_hz,
                "pulse_count": full_stats.pulse_count,
                "logic_low_v": full_stats.logic_low_v,
                "logic_high_v": full_stats.logic_high_v,
                "sample_period_s": full_stats.sample_period_s,
                "duration_s": full_stats.duration_s,
            },
        }

    def _render_current_view_image(self, *, scale: float = 2.0) -> QPixmap:
        container = self
        base_size = container.size()
        target_width = max(int(base_size.width() * scale), 1)
        target_height = max(int(base_size.height() * scale), 1)
        pixmap = QPixmap(target_width, target_height)
        pixmap.fill(QColor(self.theme.window_background))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.scale(scale, scale)
        container.render(painter, QPoint())
        painter.end()
        return pixmap

    def _decorate_export_image(self, pixmap: QPixmap) -> None:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        footer_height = 34
        footer_rect = pixmap.rect()
        footer_rect.setTop(max(footer_rect.bottom() - footer_height, 0))
        background = QColor(self.theme.panel_background)
        background.setAlpha(230)
        painter.fillRect(footer_rect, background)
        x_position = 14
        baseline = footer_rect.top() + 22
        for waveform in self.current_waveforms:
            if waveform.channel not in self.analysis_panel.visible_channels:
                continue
            painter.setPen(QColor(self.theme.channel_style(waveform.channel).color))
            label = display_channel_name(waveform.channel)
            painter.drawText(x_position, baseline, label)
            x_position += painter.fontMetrics().horizontalAdvance(label) + 18
        painter.setPen(QColor(self.theme.secondary_text))
        state = self.analysis_panel.capture_view_state()
        range_text = ""
        if state and isinstance(state.get("x_range"), tuple):
            left, right = state["x_range"]
            range_text = f"{float(left):.5g}…{float(right):.5g} s"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer_text = f"{range_text}   {stamp}".strip()
        painter.drawText(
            max(pixmap.width() - painter.fontMetrics().horizontalAdvance(footer_text) - 14, x_position),
            baseline,
            footer_text,
        )
        painter.end()

    def _clip_waveform_to_time_range(
        self,
        waveform: WaveformData,
        start_time_s: float,
        end_time_s: float,
    ) -> WaveformData | None:
        left = min(start_time_s, end_time_s)
        right = max(start_time_s, end_time_s)
        clipped_x: list[float] = []
        clipped_y: list[float] = []
        for time_value, signal_value in zip(waveform.x_values, waveform.y_values):
            if left <= time_value <= right:
                clipped_x.append(time_value)
                clipped_y.append(signal_value)
        if not clipped_x:
            return None
        preamble = waveform.preamble
        return WaveformData(
            channel=waveform.channel,
            points_mode=waveform.points_mode,
            preamble=type(preamble)(
                format_code=preamble.format_code,
                acquire_type=preamble.acquire_type,
                points=len(clipped_x),
                count=preamble.count,
                x_increment=preamble.x_increment,
                x_origin=clipped_x[0],
                x_reference=preamble.x_reference,
                y_increment=preamble.y_increment,
                y_origin=preamble.y_origin,
                y_reference=preamble.y_reference,
            ),
            x_values=clipped_x,
            y_values=clipped_y,
        )

    def _write_marker_sidecar(
        self,
        csv_path: Path,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        annotation_text: str,
    ) -> None:
        marker_path = csv_path.with_suffix(".markers.json")
        payload = {
            "annotation_text": annotation_text,
            "point_a": [point_a[0], point_a[1]],
            "point_b": [point_b[0], point_b[1]],
        }
        marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        self.cursor_detail_label.setText(
            "\n".join(f"{name}: {value}" for name, value in measurements.items())
            if measurements
            else "尚未放置游标"
        )
        self._refresh_measurement_footer()

    def _toggle_measurement_freeze(self, checked: bool) -> None:
        self._measurement_frozen = checked
        self.freeze_measurements_button.setText("取消冻结" if checked else "冻结测量")
        if not checked:
            self._refresh_measurement_footer()
            self._refresh_phase_compare_toolbar()

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

    def _save_phase_settings(self) -> None:
        WAVEFORM_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase_interval_memory": self._phase_interval_memory,
        }
        with WAVEFORM_PHASE_SETTINGS_PATH.open("w", encoding="utf-8") as settings_file:
            json.dump(payload, settings_file, ensure_ascii=False, indent=2)

    def _load_phase_settings(self) -> None:
        if not WAVEFORM_PHASE_SETTINGS_PATH.exists():
            return
        try:
            payload = json.loads(WAVEFORM_PHASE_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log_message(f"相位差配置加载失败: {exc}")
            return
        loaded_memory: dict[str, float | None] = {}
        for key, value in payload.get("phase_interval_memory", {}).items():
            if value is None:
                loaded_memory[str(key)] = None
                continue
            try:
                loaded_memory[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        self._phase_interval_memory = loaded_memory

    def _phase_interval_memory_key(self, reference_channel: str, target_channel: str, mode: str) -> str:
        return f"{reference_channel}->{target_channel}|{mode}"

    def _preferred_phase_interval(
        self,
        reference_channel: str | None,
        target_channel: str | None,
        mode: str,
        fallback: float | None,
    ) -> float | None:
        if reference_channel and target_channel and mode == "encoder_ab":
            key = self._phase_interval_memory_key(reference_channel, target_channel, mode)
            if key in self._phase_interval_memory:
                return self._phase_interval_memory[key]
        return fallback

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
        if self._measurement_frozen:
            self._reposition_measurement_overlay()
            return

        channel_sections: list[str] = []
        all_channel_sections: list[str] = []
        measurement_scope = self._selected_measurement_scope()
        for waveform in self.current_waveforms:
            section_html = self._build_measurement_section_html(waveform, measurement_scope)
            if section_html:
                all_channel_sections.append(section_html)
                if waveform.channel == self.analysis_panel.active_waveform_channel:
                    channel_sections.append(section_html)
        if not channel_sections and all_channel_sections:
            channel_sections.append(all_channel_sections[0])
        self.measurement_detail_label.setText("<br>".join(all_channel_sections))

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
                self.measurement_overlay.setVisible(self.measurement_overlay_check.isChecked())
            else:
                self.measurement_text_label.clear()
                if hint_text:
                    self.measurement_overlay_hint.setText(hint_text)
                    self.measurement_overlay.setVisible(self.measurement_overlay_check.isChecked())
                else:
                    self.measurement_overlay.hide()

            if cursor_section:
                self.cursor_overlay_hint.setText("游标测量")
                self.cursor_text_label.setText(cursor_section)
                self.cursor_overlay.setVisible(self.cursor_overlay_check.isChecked())
            else:
                self.cursor_text_label.clear()
                self.cursor_overlay.hide()

            self._reposition_measurement_overlay()

    def _selected_measurement_scope(self) -> str:
        return str(self.measurement_scope_combo.currentData())

    def _sync_phase_compare_toolbar_from_panel(self) -> None:
        options = self.analysis_panel.comparison_target_options()
        target_channel, edge_type, comparison_mode, minimum_edge_interval_s, _comparison, _message = self.analysis_panel.channel_comparison_state()
        reference_channel = self.analysis_panel.active_waveform_channel

        self.phase_channel_combo.blockSignals(True)
        self.phase_channel_combo.clear()
        self.phase_channel_combo.addItem("关闭", "")
        for channel in options:
            self.phase_channel_combo.addItem(display_channel_name(channel), channel)
        if target_channel and target_channel in options:
            index = self.phase_channel_combo.findData(target_channel)
            if index >= 0:
                self.phase_channel_combo.setCurrentIndex(index)
        self.phase_channel_combo.setEnabled(bool(options))
        self.phase_channel_combo.blockSignals(False)

        self.phase_edge_combo.blockSignals(True)
        edge_index = self.phase_edge_combo.findData(edge_type)
        if edge_index >= 0:
            self.phase_edge_combo.setCurrentIndex(edge_index)
        self.phase_edge_combo.setEnabled(bool(options))
        self.phase_edge_combo.blockSignals(False)

        self.phase_mode_combo.blockSignals(True)
        mode_index = self.phase_mode_combo.findData(comparison_mode)
        if mode_index >= 0:
            self.phase_mode_combo.setCurrentIndex(mode_index)
        self.phase_mode_combo.setEnabled(bool(options))
        self.phase_mode_combo.blockSignals(False)

        preferred_interval = self._preferred_phase_interval(
            reference_channel,
            target_channel,
            comparison_mode,
            minimum_edge_interval_s,
        )
        self.phase_interval_combo.blockSignals(True)
        interval_index = self.phase_interval_combo.findData(preferred_interval)
        if interval_index < 0:
            interval_index = 0
        self.phase_interval_combo.setCurrentIndex(interval_index)
        self.phase_interval_combo.setEnabled(comparison_mode == "encoder_ab" and bool(options))
        self.phase_interval_combo.blockSignals(False)

        if (
            comparison_mode == "encoder_ab"
            and target_channel
            and preferred_interval != minimum_edge_interval_s
            and not self._applying_saved_phase_interval
        ):
            self._applying_saved_phase_interval = True
            try:
                self.analysis_panel.set_channel_comparison(
                    target_channel,
                    edge_type,
                    comparison_mode,
                    preferred_interval,
                )
            finally:
                self._applying_saved_phase_interval = False
            return
        self._refresh_phase_compare_toolbar()

    def _sync_phase_compare_controls_to_panel(self) -> None:
        target_channel = self.phase_channel_combo.currentData()
        edge_type = str(self.phase_edge_combo.currentData() or "rising")
        comparison_mode = str(self.phase_mode_combo.currentData() or "general")
        minimum_edge_interval_s = self.phase_interval_combo.currentData()
        reference_channel = self.analysis_panel.active_waveform_channel
        if comparison_mode == "encoder_ab" and target_channel and reference_channel:
            key = self._phase_interval_memory_key(reference_channel, str(target_channel), comparison_mode)
            self._phase_interval_memory[key] = minimum_edge_interval_s
            self._save_phase_settings()
        self.analysis_panel.set_channel_comparison(
            str(target_channel) if target_channel else None,
            edge_type,
            comparison_mode,
            minimum_edge_interval_s if comparison_mode == "encoder_ab" else None,
        )

    def _refresh_phase_compare_toolbar(self) -> None:
        if self._measurement_frozen:
            return
        target_channel, edge_type, comparison_mode, minimum_edge_interval_s, comparison, message = self.analysis_panel.channel_comparison_state()
        reference_channel = self.analysis_panel.active_waveform_channel
        if not target_channel:
            self.phase_result_label.setText("相位差: --")
            self.phase_metrics_label.clear()
            return
        mode_label = "编码器AB" if comparison_mode == "encoder_ab" else "通用"
        relation_text = (
            f"{display_channel_name(target_channel)} 相对 {display_channel_name(reference_channel)}"
            if reference_channel
            else display_channel_name(target_channel)
        )
        if comparison is None:
            self.phase_result_label.setText(
                f"{relation_text} / {mode_label} / {'上升沿' if edge_type == 'rising' else '下降沿'}: {message or '无法估算'}"
            )
            self.phase_metrics_label.clear()
            return
        dt_text = format_engineering_value(comparison.delta_t_s, "s")
        phase_text = f"{comparison.phase_deg:.2f}°" if comparison.phase_deg is not None else "--"
        suffix = ""
        metrics_text = ""
        if comparison_mode == "encoder_ab":
            interval_text = "自动" if minimum_edge_interval_s is None else format_engineering_value(minimum_edge_interval_s, "s")
            suffix = f"  可信度 {comparison.confidence or '--'}"
            metrics_text = (
                f"总跳变 {comparison.raw_transition_count}  "
                f"去抖过滤 {comparison.debounce_filtered_count}  "
                f"序列丢弃 {comparison.sequence_discarded_count}  "
                f"合法/非法 {comparison.valid_transition_count}/{comparison.invalid_transition_count}  "
                f"最小间隔 {interval_text}"
            )
        self.phase_result_label.setText(
            f"{relation_text} / {mode_label}  Δt {dt_text}  相位差 {phase_text}{suffix}  {message or ''}".rstrip()
        )
        self.phase_metrics_label.setText(metrics_text)

    def _measurement_stats_for_channel(self, channel: str, measurement_scope: str) -> WaveformStats | None:
        if measurement_scope == "cursor":
            return self.analysis_panel.cursor_window_stats_for_channel(channel)
        if measurement_scope == "full":
            return self.analysis_panel.full_stats_for_channel(channel)
        return self.analysis_panel.visible_stats_for_channel(channel)

    def _build_measurement_section_html(self, waveform: WaveformData, measurement_scope: str = "view") -> str:
        stats = self._measurement_stats_for_channel(waveform.channel, measurement_scope)
        if stats is None:
            stats = waveform.analyze()
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
                f"<span style='color:{self.theme.channel_style(waveform.channel).color};'>"
                f"{measurement_name}"
                "</span>"
                f"<span style='color:{self.theme.channel_style(waveform.channel).color};'>: </span>"
                f"<span style='font-weight:600; color:{self.theme.channel_style(waveform.channel).color};'>"
                f"{formatted_value}"
                "</span>"
            )

        if not metric_items:
            return ""

        title = display_channel_name(waveform.channel)
        title_color = self.theme.channel_style(waveform.channel).color
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
        core_labels = {"游标 A", "游标 B", "Δt", "ΔV/ΔI"}
        visible_items = [
            (label, value)
            for label, value in self.cursor_measurements.items()
            if label in core_labels and value and value != "-"
        ]
        target_channel, edge_type, comparison_mode, minimum_edge_interval_s, comparison, message = self.analysis_panel.channel_comparison_state()
        reference_channel = self.analysis_panel.active_waveform_channel
        include_phase_in_overlay = False
        if include_phase_in_overlay and target_channel:
            relation_label = (
                f"{display_channel_name(target_channel)} 相对 {display_channel_name(reference_channel)}"
                if reference_channel
                else display_channel_name(target_channel)
            )
            mode_label = "编码器AB" if comparison_mode == "encoder_ab" else "通用"
            if comparison is None:
                visible_items.append(
                    (
                        f"相位差({relation_label} / {mode_label} / {'上升沿' if edge_type == 'rising' else '下降沿'})",
                        message or "无法估算",
                    )
                )
            else:
                phase_text = f"{comparison.phase_deg:.2f}°" if comparison.phase_deg is not None else "--"
                dt_text = format_engineering_value(comparison.delta_t_s, "s")
                extra_text = ""
                if comparison_mode == "encoder_ab":
                    interval_text = "自动" if minimum_edge_interval_s is None else format_engineering_value(minimum_edge_interval_s, "s")
                    extra_text = (
                        f" / 可信度 {comparison.confidence or '--'}"
                        f" / 总跳变 {comparison.raw_transition_count}"
                        f" / 去抖过滤 {comparison.debounce_filtered_count}"
                        f" / 序列丢弃 {comparison.sequence_discarded_count}"
                        f" / 合法/非法 {comparison.valid_transition_count}/{comparison.invalid_transition_count}"
                        f" / 最小间隔 {interval_text}"
                    )
                visible_items.append(
                    (
                        f"相位差({relation_label} / {mode_label} / {'上升沿' if edge_type == 'rising' else '下降沿'})",
                        f"{phase_text} / Δt {dt_text}{extra_text}" + (f" / {message}" if message else ""),
                    )
                )
        if not visible_items:
            return ""

        rows: list[str] = []
        for label, value in visible_items:
            rows.append(
                "<tr>"
                f"<td style='padding:0 0 10px 0; vertical-align:top; white-space:nowrap; line-height:1.62; font-size:{OVERLAY_BODY_HTML_PX}px;'>"
                f"<span style='color:{self.theme.secondary_text};'>{label}</span>"
                f"<br><span style='font-weight:600; color:{self.theme.primary_text};'>{value}</span>"
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
            overlay_width = min(
                max(int(chart_rect.width() * 0.28), 300),
                min(max(chart_rect.width() - 44, 260), 440),
            )
            self.measurement_overlay.setMinimumWidth(260)
            self.measurement_overlay.setMaximumWidth(max(overlay_width, 260))
            self.measurement_text_label.setMinimumWidth(220)
            self.measurement_text_label.setMaximumWidth(max(overlay_width - 16, 220))
            self.measurement_text_label.adjustSize()
            content_height = (
                0
                if self.measurement_text_label.isHidden()
                else self.measurement_text_label.sizeHint().height()
            )
            hint_height = max(
                self.measurement_overlay_hint.sizeHint().height(),
                self.measurement_overlay_collapse_button.sizeHint().height(),
            )
            overlay_height = hint_height + content_height + 18
            x_pos = 24
            y_pos = max(chart_rect.height() - overlay_height - 28, 48)
            self.measurement_overlay.setGeometry(x_pos, y_pos, overlay_width, overlay_height)
            self.measurement_overlay.raise_()

        if self.cursor_overlay.isVisible():
            cursor_width = min(max(int(chart_rect.width() * 0.2), 180), 290)
            self.cursor_overlay.setMinimumWidth(160)
            self.cursor_overlay.setMaximumWidth(cursor_width)
            self.cursor_text_label.setMinimumWidth(140)
            self.cursor_text_label.setMaximumWidth(max(cursor_width - 10, 140))
            self.cursor_text_label.adjustSize()
            cursor_content_height = (
                0
                if self.cursor_text_label.isHidden()
                else self.cursor_text_label.sizeHint().height()
            )
            cursor_hint_height = max(
                self.cursor_overlay_hint.sizeHint().height(),
                self.cursor_overlay_collapse_button.sizeHint().height(),
            )
            cursor_height = cursor_hint_height + cursor_content_height + 12
            cursor_x = max(chart_rect.width() - cursor_width - 18, 12)
            cursor_y = 24
            self.cursor_overlay.setGeometry(cursor_x, cursor_y, cursor_width, cursor_height)
            self.cursor_overlay.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._reposition_measurement_overlay()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_workspace_settings()
        super().closeEvent(event)

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
        apply_responsive_window_geometry(
            self,
            minimum_width=760,
            minimum_height=540,
            preferred_width=1440,
            preferred_height=920,
        )
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        content = QWidget(self)
        layout = QVBoxLayout(content)
        outer_layout.addWidget(create_scroll_area(self, content, minimum_px=960, minimum_chars=112))
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
