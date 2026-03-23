from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
import sys
import threading

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QTextCursor
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
    QDialog,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsSimpleTextItem,
)

from keysight_scope_app.instrument import (
    MEASUREMENT_DEFINITIONS,
    SUPPORTED_CHANNELS,
    SUPPORTED_WAVEFORM_POINTS_MODES,
    KeysightOscilloscope,
    StartupBrakeTestConfig,
    StartupBrakeTestResult,
    WaveformData,
    WaveformStats,
    analyze_startup_brake_test,
    compare_waveform_edges,
    list_visa_resources,
)


CAPTURE_DIR = Path("captures")
WAVEFORM_DIR = Path("captures") / "waveforms"
WAVEFORM_IMAGE_DIR = Path("captures") / "waveform_images"
STARTUP_BRAKE_DIR = Path("captures") / "startup_brake_tests"
MAX_LOG_LINES = 300
DEFAULT_MEASUREMENT_SET = {"频率", "峰峰值", "均方根"}
WAVEFORM_SERIES_COLORS = ("#2d9cdb", "#eb5757", "#27ae60", "#f2994a")
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


class InteractiveChartView(QChartView):
    def __init__(self, chart: QChart, parent: QWidget | None = None) -> None:
        super().__init__(chart, parent)
        self.point_click_callback = None
        self.hover_cursor_callback = None
        self.drag_start_callback = None
        self.drag_move_callback = None
        self.drag_end_callback = None
        self.reset_view_callback = None
        self.default_x_range: tuple[float, float] | None = None
        self.default_y_range: tuple[float, float] | None = None
        self._drag_callback_active = False
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setMouseTracking(True)
        self.setRubberBand(QChartView.RectangleRubberBand)

        pen = QPen(QColor("#d94f4f"))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        self.crosshair_x = QGraphicsLineItem()
        self.crosshair_x.setPen(pen)
        self.crosshair_y = QGraphicsLineItem()
        self.crosshair_y.setPen(pen)
        self.crosshair_label = QGraphicsSimpleTextItem()
        self.crosshair_label.setBrush(QColor("#16324f"))
        self.crosshair_label.setVisible(False)
        self.chart().scene().addItem(self.crosshair_x)
        self.chart().scene().addItem(self.crosshair_y)
        self.chart().scene().addItem(self.crosshair_label)
        self._hide_crosshair()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self.hover_cursor_callback is not None:
            self.setCursor(self.hover_cursor_callback(event.position()))
        if self._drag_callback_active and self.drag_move_callback is not None:
            value, _ = self._map_position_to_plot_value(event.position())
            if self.drag_move_callback(value.x(), value.y(), event.position()):
                self._update_crosshair(event.position())
                event.accept()
                return
        super().mouseMoveEvent(event)
        self._update_crosshair(event.position())

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hide_crosshair()
        self.unsetCursor()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        factor = 1.18 if event.angleDelta().y() > 0 else 0.85
        modifiers = event.modifiers()
        value = self.chart().mapToValue(event.position().toPoint())
        if modifiers & Qt.ShiftModifier:
            self._zoom_axis("x", factor, value.x())
        elif modifiers & Qt.ControlModifier:
            self._zoom_axis("y", factor, value.y())
        else:
            self.chart().zoom(factor)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.RightButton:
            if self.reset_view_callback is not None:
                self.reset_view_callback()
            else:
                self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            position = event.position()
            value, inside_plot = self._map_position_to_plot_value(position)
            if inside_plot and self.point_click_callback is not None:
                if self.point_click_callback(value.x(), value.y(), position):
                    event.accept()
                    return
            if self.drag_start_callback is not None:
                if self.drag_start_callback(value.x(), value.y(), position):
                    self._drag_callback_active = True
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._drag_callback_active:
            self._drag_callback_active = False
            if self.drag_end_callback is not None:
                value, _ = self._map_position_to_plot_value(event.position())
                self.drag_end_callback(value.x(), value.y(), event.position())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def reset_view(self) -> None:
        self.chart().zoomReset()
        self._hide_crosshair()

    def reset_horizontal(self) -> None:
        if self.default_x_range is None:
            return
        axis_x = self._x_axis()
        if axis_x is None:
            return
        axis_x.setRange(*self.default_x_range)

    def reset_vertical(self) -> None:
        if self.default_y_range is None:
            return
        axis_y = self._y_axis()
        if axis_y is None:
            return
        axis_y.setRange(*self.default_y_range)

    def set_default_ranges(self, x_range: tuple[float, float], y_range: tuple[float, float]) -> None:
        self.default_x_range = x_range
        self.default_y_range = y_range

    def _zoom_axis(self, axis_name: str, factor: float, focus_value: float) -> None:
        axis = self._x_axis() if axis_name == "x" else self._y_axis()
        if axis is None:
            return

        minimum = axis.min()
        maximum = axis.max()
        span = maximum - minimum
        if span <= 0:
            return

        new_span = span / factor
        ratio = 0.5 if maximum == minimum else (focus_value - minimum) / span
        ratio = max(0.0, min(1.0, ratio))
        new_min = focus_value - new_span * ratio
        new_max = new_min + new_span
        axis.setRange(new_min, new_max)

    def _x_axis(self) -> QValueAxis | None:
        for axis in self.chart().axes(Qt.Horizontal):
            if isinstance(axis, QValueAxis):
                return axis
        return None

    def _y_axis(self) -> QValueAxis | None:
        for axis in self.chart().axes(Qt.Vertical):
            if isinstance(axis, QValueAxis):
                return axis
        return None

    def _update_crosshair(self, position) -> None:
        plot_area = self.chart().plotArea()
        if not plot_area.contains(position):
            self._hide_crosshair()
            return

        scene_position = self.mapToScene(position.toPoint())
        value = self.chart().mapToValue(position.toPoint())
        self.crosshair_x.setLine(plot_area.left(), scene_position.y(), plot_area.right(), scene_position.y())
        self.crosshair_y.setLine(scene_position.x(), plot_area.top(), scene_position.x(), plot_area.bottom())
        self.crosshair_x.setVisible(True)
        self.crosshair_y.setVisible(True)

        self.crosshair_label.setText(f"t={value.x():.6e} s\nV={value.y():.6f} V")
        label_x = min(scene_position.x() + 12, plot_area.right() - 120)
        label_y = max(scene_position.y() - 36, plot_area.top() + 8)
        self.crosshair_label.setPos(QPointF(label_x, label_y))
        self.crosshair_label.setVisible(True)

    def _hide_crosshair(self) -> None:
        self.crosshair_x.setVisible(False)
        self.crosshair_y.setVisible(False)
        self.crosshair_label.setVisible(False)

    def _map_position_to_plot_value(self, position) -> tuple[QPointF, bool]:
        plot_area = self.chart().plotArea()
        inside_plot = plot_area.contains(position)
        clamped_x = min(max(position.x(), plot_area.left()), plot_area.right())
        clamped_y = min(max(position.y(), plot_area.top()), plot_area.bottom())
        mapped = self.chart().mapToValue(QPointF(clamped_x, clamped_y).toPoint())
        return mapped, inside_plot


class WaveformAnalysisPanel(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "尚未加载波形",
        compact_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.compact_mode = compact_mode
        self.current_waveforms: list[WaveformData] = []
        self.current_waveform: WaveformData | None = None
        self.current_stats: WaveformStats | None = None
        self.waveform_series: QLineSeries | None = None
        self.waveform_series_map: dict[str, QLineSeries] = {}
        self.waveform_decimated_map: dict[str, tuple[list[float], list[float]]] = {}
        self.waveform_offsets: dict[str, float] = {}
        self.pending_cursor_target: str | None = None
        self.dragging_cursor_target: tuple[str, str] | None = None
        self.hover_cursor_target: tuple[str, str] | None = None
        self.dragging_waveform_channel: str | None = None
        self.hover_waveform_channel: str | None = None
        self.waveform_drag_anchor_y: float = 0.0
        self.waveform_drag_initial_offset: float = 0.0
        self.cursor_points: dict[str, tuple[float, float]] = {}
        self.lock_annotation_text: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        chart_toolbar = QHBoxLayout()
        chart_toolbar.setSpacing(10)
        self.reset_view_button = QPushButton("重置视图")
        self.reset_x_button = QPushButton("横向重置")
        self.reset_y_button = QPushButton("纵向重置")
        self.reset_offsets_button = QPushButton("重置分离")
        self.export_image_button = QPushButton("导出图像")
        self.help_label = QLabel(
            "滚轮双轴缩放，Shift+滚轮仅缩放时间轴，Ctrl+滚轮仅缩放电压轴，左键框选局部放大，右键双击重置。"
        )
        self.help_label.setWordWrap(True)
        if self.compact_mode:
            chart_toolbar.addWidget(
                self._build_control_group("视图", [self.reset_view_button, self.reset_x_button, self.reset_y_button, self.export_image_button], columns=2)
            )
        else:
            chart_toolbar.addWidget(
                self._build_control_group("视图", [self.reset_view_button, self.reset_x_button, self.reset_y_button, self.reset_offsets_button], columns=2)
            )
            chart_toolbar.addWidget(
                self._build_control_group("导出", [self.export_image_button])
            )
        help_card = QFrame()
        help_card.setFrameShape(QFrame.StyledPanel)
        help_layout = QVBoxLayout(help_card)
        help_layout.setContentsMargins(10, 8, 10, 8)
        help_title = QLabel("操作提示")
        help_title.setFont(QFont(help_title.font().family(), help_title.font().pointSize(), QFont.Bold))
        help_layout.addWidget(help_title)
        help_layout.addWidget(self.help_label)
        chart_toolbar.addWidget(help_card, 1)
        layout.addLayout(chart_toolbar)

        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.setTitle(title)
        self.chart_view = InteractiveChartView(self.chart)
        self.chart_view.point_click_callback = self._handle_chart_click
        self.chart_view.hover_cursor_callback = self._hover_cursor_shape
        self.chart_view.drag_start_callback = self._handle_chart_drag_start
        self.chart_view.drag_move_callback = self._handle_chart_drag_move
        self.chart_view.drag_end_callback = self._handle_chart_drag_end
        self.chart_view.reset_view_callback = self._reset_visual_view
        self.chart_view.setMinimumHeight(220 if self.compact_mode else 520)
        layout.addWidget(self.chart_view)

        cursor_toolbar = QHBoxLayout()
        cursor_toolbar.setSpacing(10)
        self.cursor_a_button = QPushButton("设置游标 A")
        self.cursor_b_button = QPushButton("设置游标 B")
        self.cursor_a_rise_button = QPushButton("A 吸附上升沿")
        self.cursor_a_fall_button = QPushButton("A 吸附下降沿")
        self.cursor_b_rise_button = QPushButton("B 吸附上升沿")
        self.cursor_b_fall_button = QPushButton("B 吸附下降沿")
        self.smart_lock_button = QPushButton("智能锁定")
        self.lock_pulse_button = QPushButton("锁定最近脉冲")
        self.lock_period_button = QPushButton("锁定最近周期")
        self.clear_cursor_button = QPushButton("清除游标")
        self.cursor_hint_label = QLabel("提示：点击“设置游标 A/B”后，在图上单击放置；拖动竖线改时间，拖动横线改电压，拖动交点同时改两者。")
        self.cursor_hint_label.setWordWrap(True)
        if self.compact_mode:
            cursor_toolbar.addWidget(
                self._build_control_group(
                    "快速分析",
                    [self.smart_lock_button, self.lock_pulse_button, self.lock_period_button, self.clear_cursor_button],
                    columns=2,
                )
            )
        else:
            cursor_toolbar.addWidget(
                self._build_control_group(
                    "游标定位",
                    [
                        self.cursor_a_button,
                        self.cursor_b_button,
                        self.cursor_a_rise_button,
                        self.cursor_a_fall_button,
                        self.cursor_b_rise_button,
                        self.cursor_b_fall_button,
                    ],
                    columns=3,
                )
            )
            cursor_toolbar.addWidget(
                self._build_control_group(
                    "锁定策略",
                    [self.smart_lock_button, self.lock_pulse_button, self.lock_period_button, self.clear_cursor_button],
                    columns=2,
                )
            )
        hint_card = QFrame()
        hint_card.setFrameShape(QFrame.StyledPanel)
        hint_layout = QVBoxLayout(hint_card)
        hint_layout.setContentsMargins(10, 8, 10, 8)
        hint_title = QLabel("当前状态")
        hint_title.setFont(QFont(hint_title.font().family(), hint_title.font().pointSize(), QFont.Bold))
        hint_layout.addWidget(hint_title)
        hint_layout.addWidget(self.cursor_hint_label)
        cursor_toolbar.addWidget(hint_card, 1)
        layout.addLayout(cursor_toolbar)

        kpi_title = QLabel("当前视图关键指标")
        kpi_title.setFont(QFont(kpi_title.font().family(), kpi_title.font().pointSize(), QFont.Bold))
        layout.addWidget(kpi_title)

        kpi_grid = QGridLayout()
        kpi_grid.setHorizontalSpacing(8)
        kpi_grid.setVerticalSpacing(8)
        self.view_kpi_labels: dict[str, QLabel] = {}
        kpi_items = [
            ("峰峰值", "vpp"),
            ("频率", "frequency"),
            ("脉宽", "pulse_width"),
            ("RMS", "rms"),
        ]
        for index, (title, key) in enumerate(kpi_items):
            card = QFrame()
            card.setFrameShape(QFrame.StyledPanel)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            title_label = QLabel(title)
            title_label.setAlignment(Qt.AlignCenter)
            value_label = QLabel("-")
            value_label.setWordWrap(True)
            value_font = value_label.font()
            value_font.setBold(True)
            value_font.setPointSize(max(value_font.pointSize(), 12))
            value_label.setFont(value_font)
            value_label.setAlignment(Qt.AlignCenter)
            self.view_kpi_labels[key] = value_label
            card_layout.addWidget(title_label)
            card_layout.addWidget(value_label)
            kpi_grid.addWidget(card, 0, index)
        layout.addLayout(kpi_grid)

        stats_tabs = QTabWidget()

        self.cursor_labels = {
            "a": QLabel("-"),
            "b": QLabel("-"),
            "dt": QLabel("-"),
            "dv": QLabel("-"),
            "slope": QLabel("-"),
            "frequency": QLabel("-"),
        }
        cursor_items = [
            ("游标 A", "a"),
            ("游标 B", "b"),
            ("Δt", "dt"),
            ("ΔV", "dv"),
            ("ΔV/Δt", "slope"),
            ("1/Δt", "frequency"),
        ]
        cursor_tab = self._build_metric_grid(self.cursor_labels, cursor_items, columns=4)
        stats_tabs.addTab(cursor_tab, "游标")

        self.stats_labels = {
            "point_count": QLabel("-"),
            "duration": QLabel("-"),
            "sample_period": QLabel("-"),
            "vpp": QLabel("-"),
            "vmin": QLabel("-"),
            "vmax": QLabel("-"),
            "mean": QLabel("-"),
            "rms": QLabel("-"),
            "frequency": QLabel("-"),
            "pulse_width": QLabel("-"),
            "duty": QLabel("-"),
            "rise_time": QLabel("-"),
            "fall_time": QLabel("-"),
        }
        stats_items = [
            ("点数", "point_count"),
            ("时长", "duration"),
            ("采样间隔", "sample_period"),
            ("峰峰值", "vpp"),
            ("最小值", "vmin"),
            ("最大值", "vmax"),
            ("平均值", "mean"),
            ("RMS", "rms"),
            ("估算频率", "frequency"),
            ("脉宽", "pulse_width"),
            ("占空比", "duty"),
            ("上升时间", "rise_time"),
            ("下降时间", "fall_time"),
        ]
        stats_tab = self._build_metric_grid(self.stats_labels, stats_items, columns=4)
        stats_tabs.addTab(stats_tab, "全局统计")

        self.view_stats_labels = {
            "points": QLabel("-"),
            "duration": QLabel("-"),
            "vpp": QLabel("-"),
            "rms": QLabel("-"),
            "frequency": QLabel("-"),
            "pulse_width": QLabel("-"),
            "duty": QLabel("-"),
            "rise_time": QLabel("-"),
        }
        view_stats_items = [
            ("点数", "points"),
            ("时长", "duration"),
            ("峰峰值", "vpp"),
            ("RMS", "rms"),
            ("估算频率", "frequency"),
            ("脉宽", "pulse_width"),
            ("占空比", "duty"),
            ("上升时间", "rise_time"),
        ]
        view_stats_tab = self._build_metric_grid(self.view_stats_labels, view_stats_items, columns=4)
        stats_tabs.addTab(view_stats_tab, "当前视图统计")

        compare_tab = QWidget()
        compare_layout = QVBoxLayout(compare_tab)
        compare_layout.setContentsMargins(8, 8, 8, 8)
        compare_layout.setSpacing(8)
        compare_controls = QHBoxLayout()
        compare_controls.addWidget(QLabel("对比通道"))
        self.compare_channel_combo = QComboBox()
        self.compare_channel_combo.setEnabled(False)
        compare_controls.addWidget(self.compare_channel_combo)
        compare_controls.addSpacing(12)
        compare_controls.addWidget(QLabel("边沿类型"))
        self.compare_edge_combo = QComboBox()
        self.compare_edge_combo.addItem("上升沿", "rising")
        self.compare_edge_combo.addItem("下降沿", "falling")
        compare_controls.addWidget(self.compare_edge_combo)
        compare_controls.addStretch(1)
        compare_layout.addLayout(compare_controls)
        self.compare_labels = {
            "primary_channel": QLabel("-"),
            "secondary_channel": QLabel("-"),
            "primary_edge": QLabel("-"),
            "secondary_edge": QLabel("-"),
            "delta_t": QLabel("-"),
            "phase": QLabel("-"),
            "frequency": QLabel("-"),
            "edge_type": QLabel("-"),
        }
        compare_items = [
            ("主通道", "primary_channel"),
            ("对比通道", "secondary_channel"),
            ("主边沿时间", "primary_edge"),
            ("对比边沿时间", "secondary_edge"),
            ("Δt", "delta_t"),
            ("相位差", "phase"),
            ("参考频率", "frequency"),
            ("边沿类型", "edge_type"),
        ]
        compare_layout.addWidget(self._build_metric_grid(self.compare_labels, compare_items, columns=4))
        stats_tabs.addTab(compare_tab, "通道对比")
        if self.compact_mode:
            stats_tabs.setCurrentIndex(2)
            stats_tabs.setMaximumHeight(210)
        else:
            stats_tabs.setMaximumHeight(260)

        layout.addWidget(stats_tabs)
        layout.setStretch(0, 0)
        layout.setStretch(1, 5)
        layout.setStretch(2, 0)
        layout.setStretch(3, 0)
        layout.setStretch(4, 0)
        layout.setStretch(5, 1)

        self.reset_view_button.clicked.connect(self._reset_visual_view)
        self.reset_x_button.clicked.connect(self.chart_view.reset_horizontal)
        self.reset_y_button.clicked.connect(self.chart_view.reset_vertical)
        self.reset_offsets_button.clicked.connect(self._reset_waveform_offsets)
        self.export_image_button.clicked.connect(self._export_chart_image)
        self.cursor_a_button.clicked.connect(lambda: self._arm_cursor("a"))
        self.cursor_b_button.clicked.connect(lambda: self._arm_cursor("b"))
        self.cursor_a_rise_button.clicked.connect(lambda: self._snap_cursor_to_edge("a", "rising"))
        self.cursor_a_fall_button.clicked.connect(lambda: self._snap_cursor_to_edge("a", "falling"))
        self.cursor_b_rise_button.clicked.connect(lambda: self._snap_cursor_to_edge("b", "rising"))
        self.cursor_b_fall_button.clicked.connect(lambda: self._snap_cursor_to_edge("b", "falling"))
        self.smart_lock_button.clicked.connect(self._smart_lock_window)
        self.lock_pulse_button.clicked.connect(self._lock_nearest_pulse)
        self.lock_period_button.clicked.connect(self._lock_nearest_period)
        self.clear_cursor_button.clicked.connect(self._clear_cursors)
        self.chart.plotAreaChanged.connect(lambda _: self._refresh_cursor_graphics())
        self.compare_channel_combo.currentIndexChanged.connect(lambda _: self._update_channel_comparison())
        self.compare_edge_combo.currentIndexChanged.connect(lambda _: self._update_channel_comparison())

        cursor_pen_a = QPen(QColor("#1f77b4"))
        cursor_pen_a.setWidth(2)
        cursor_pen_b = QPen(QColor("#ff7f0e"))
        cursor_pen_b.setWidth(2)
        self.cursor_line_items = {
            "a": QGraphicsLineItem(),
            "b": QGraphicsLineItem(),
        }
        self.cursor_line_items["a"].setPen(cursor_pen_a)
        self.cursor_line_items["b"].setPen(cursor_pen_b)
        self.cursor_hline_items = {
            "a": QGraphicsLineItem(),
            "b": QGraphicsLineItem(),
        }
        self.cursor_hline_items["a"].setPen(cursor_pen_a)
        self.cursor_hline_items["b"].setPen(cursor_pen_b)
        self.cursor_handle_items = {
            "a": QGraphicsEllipseItem(),
            "b": QGraphicsEllipseItem(),
        }
        self.cursor_text_items = {
            "a": QGraphicsSimpleTextItem(),
            "b": QGraphicsSimpleTextItem(),
        }
        self.cursor_mode_items = {
            "a": QGraphicsSimpleTextItem(),
            "b": QGraphicsSimpleTextItem(),
        }
        self.cursor_text_items["a"].setBrush(QColor("#1f77b4"))
        self.cursor_text_items["b"].setBrush(QColor("#ff7f0e"))
        self.cursor_mode_items["a"].setBrush(QColor("#1f77b4"))
        self.cursor_mode_items["b"].setBrush(QColor("#ff7f0e"))
        for key in ("a", "b"):
            self.chart.scene().addItem(self.cursor_line_items[key])
            self.chart.scene().addItem(self.cursor_hline_items[key])
            self.chart.scene().addItem(self.cursor_handle_items[key])
            self.chart.scene().addItem(self.cursor_text_items[key])
            self.chart.scene().addItem(self.cursor_mode_items[key])

        annotation_pen = QPen(QColor("#2a9d6f"))
        annotation_pen.setWidth(2)
        self.lock_annotation_line = QGraphicsLineItem()
        self.lock_annotation_line.setPen(annotation_pen)
        self.lock_annotation_text_item = QGraphicsSimpleTextItem()
        self.lock_annotation_text_item.setBrush(QColor("#2a9d6f"))
        self.chart.scene().addItem(self.lock_annotation_line)
        self.chart.scene().addItem(self.lock_annotation_text_item)
        self._clear_cursors()

    def _build_control_group(self, title: str, widgets: list[QWidget], columns: int = 3) -> QFrame:
        group = QFrame()
        group.setFrameShape(QFrame.StyledPanel)
        outer = QVBoxLayout(group)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)
        title_label = QLabel(title)
        title_label.setFont(QFont(title_label.font().family(), title_label.font().pointSize(), QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(title_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for index, widget in enumerate(widgets):
            if isinstance(widget, QPushButton):
                widget.setMinimumWidth(108)
            grid.addWidget(widget, index // columns, index % columns)
        outer.addLayout(grid)
        return group

    def _build_metric_grid(
        self,
        labels: dict[str, QLabel],
        items: list[tuple[str, str]],
        *,
        columns: int = 4,
    ) -> QWidget:
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(14)

        for index, (item_title, key) in enumerate(items):
            value_label = labels[key]
            value_label.setWordWrap(True)
            value_font = value_label.font()
            value_font.setBold(True)
            value_font.setPointSize(max(value_font.pointSize(), 11))
            value_label.setFont(value_font)
            value_label.setAlignment(Qt.AlignCenter)

            block = QWidget()
            card_layout = QVBoxLayout(block)
            card_layout.setContentsMargins(2, 2, 2, 2)
            card_layout.setSpacing(2)

            title_label = QLabel(item_title)
            title_label.setAlignment(Qt.AlignCenter)
            card_layout.addWidget(title_label)
            card_layout.addWidget(value_label)
            card_layout.addStretch(1)

            grid.addWidget(block, index // columns, index % columns)

        return container

    def set_waveform(self, waveform: WaveformData, stats: WaveformStats) -> None:
        self.set_waveforms([waveform], stats)

    def set_waveforms(self, waveforms: list[WaveformData], primary_stats: WaveformStats | None = None) -> None:
        if not waveforms:
            self.clear()
            return

        self.current_waveforms = list(waveforms)
        self.current_waveform = waveforms[0]
        self.current_stats = primary_stats or self.current_waveform.analyze()
        self.waveform_series_map = {}
        self.waveform_decimated_map = {}
        self.waveform_offsets = {waveform.channel: 0.0 for waveform in waveforms}
        self.dragging_waveform_channel = None
        self.hover_waveform_channel = None
        self._populate_compare_channels()
        self.chart.removeAllSeries()
        for axis in self.chart.axes():
            self.chart.removeAxis(axis)

        axis_x = QValueAxis()
        axis_x.setTitleText("Time (s)")
        axis_x.setLabelFormat("%.4g")
        axis_y = QValueAxis()
        axis_y.setTitleText("Voltage (V)")
        axis_y.setLabelFormat("%.4g")
        self.chart.addAxis(axis_x, Qt.AlignBottom)
        self.chart.addAxis(axis_y, Qt.AlignLeft)
        axis_x.rangeChanged.connect(self._handle_axis_range_changed)

        all_x_values: list[float] = []
        all_y_values: list[float] = []
        for index, waveform in enumerate(waveforms):
            series = QLineSeries()
            series.setName(_display_channel_name(waveform.channel))
            series.setPen(self._waveform_series_pen(waveform.channel))

            x_values, y_values = _decimate_xy(waveform.x_values, waveform.y_values, max_points=2500)
            self.waveform_decimated_map[waveform.channel] = (x_values, y_values)
            self.chart.addSeries(series)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
            self.waveform_series_map[waveform.channel] = series
            all_x_values.extend(x_values)
            all_y_values.extend(y_values)
            if index == 0:
                self.waveform_series = series

        self._render_all_waveform_series()

        self.chart.legend().setVisible(len(waveforms) > 1)
        self.chart.setTitle(" / ".join(_display_channel_name(waveform.channel) for waveform in waveforms) + " 波形")

        if all_x_values:
            axis_x.setRange(min(all_x_values), max(all_x_values))
        if all_y_values:
            y_min = min(all_y_values)
            y_max = max(all_y_values)
            if y_min == y_max:
                padding = 0.1 if y_min == 0 else abs(y_min) * 0.1
                axis_y.setRange(y_min - padding, y_max + padding)
            else:
                padding = (y_max - y_min) * 0.05
                axis_y.setRange(y_min - padding, y_max + padding)
        self.chart_view.set_default_ranges((axis_x.min(), axis_x.max()), (axis_y.min(), axis_y.max()))

        self.chart_view.reset_view()
        self._update_stats(self.current_stats)
        self._update_view_stats_from_axes()
        self._refresh_cursor_graphics()

    def clear(self) -> None:
        self.current_waveforms = []
        self.current_waveform = None
        self.current_stats = None
        self.waveform_series = None
        self.waveform_series_map = {}
        self.waveform_decimated_map = {}
        self.waveform_offsets = {}
        self.dragging_waveform_channel = None
        self.hover_waveform_channel = None
        self.compare_channel_combo.blockSignals(True)
        self.compare_channel_combo.clear()
        self.compare_channel_combo.blockSignals(False)
        self.compare_channel_combo.setEnabled(False)
        self.chart.removeAllSeries()
        for axis in self.chart.axes():
            self.chart.removeAxis(axis)
        self.chart.setTitle("尚未加载波形")
        self.chart.legend().hide()
        for label in self.stats_labels.values():
            label.setText("-")
        for label in self.view_stats_labels.values():
            label.setText("-")
        for label in self.view_kpi_labels.values():
            label.setText("-")
        for label in self.compare_labels.values():
            label.setText("-")
        self.chart_view.reset_view()
        self._clear_cursors()

    def set_cursor_points(
        self,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        *,
        annotation_text: str | None = None,
    ) -> None:
        if self.current_waveform is None:
            return
        self.pending_cursor_target = None
        self.dragging_cursor_target = None
        self.hover_cursor_target = None
        self.cursor_points["a"] = self._clamp_value_to_axes(point_a)
        self.cursor_points["b"] = self._clamp_value_to_axes(point_b)
        self.lock_annotation_text = annotation_text
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _update_stats(self, stats: WaveformStats) -> None:
        self.stats_labels["point_count"].setText(str(stats.point_count))
        self.stats_labels["duration"].setText(f"{stats.duration_s:.6e} s")
        self.stats_labels["sample_period"].setText(f"{stats.sample_period_s:.6e} s")
        self.stats_labels["vpp"].setText(f"{stats.voltage_pp:.6f} V")
        self.stats_labels["vmin"].setText(f"{stats.voltage_min:.6f} V")
        self.stats_labels["vmax"].setText(f"{stats.voltage_max:.6f} V")
        self.stats_labels["mean"].setText(f"{stats.voltage_mean:.6f} V")
        self.stats_labels["rms"].setText(f"{stats.voltage_rms:.6f} V")
        if stats.estimated_frequency_hz is None:
            self.stats_labels["frequency"].setText("无法估算")
        else:
            self.stats_labels["frequency"].setText(f"{stats.estimated_frequency_hz:.6f} Hz")
        self.stats_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.stats_labels["duty"].setText(_format_optional_percent(stats.duty_cycle))
        self.stats_labels["rise_time"].setText(_format_optional_seconds(stats.rise_time_s))
        self.stats_labels["fall_time"].setText(_format_optional_seconds(stats.fall_time_s))

    def _handle_axis_range_changed(self, minimum: float, maximum: float) -> None:
        self._update_view_stats_from_axes()
        self._update_channel_comparison()

    def _update_view_stats_from_axes(self) -> None:
        if self.current_waveform is None:
            for label in self.view_stats_labels.values():
                label.setText("-")
            for label in self.view_kpi_labels.values():
                label.setText("-")
            return

        x_axis = self._x_axis()
        if x_axis is None:
            for label in self.view_stats_labels.values():
                label.setText("-")
            for label in self.view_kpi_labels.values():
                label.setText("-")
            return

        stats = self.current_waveform.analyze_window(x_axis.min(), x_axis.max())
        if stats is None:
            for label in self.view_stats_labels.values():
                label.setText("不足")
            for label in self.view_kpi_labels.values():
                label.setText("不足")
            return

        self.view_stats_labels["points"].setText(str(stats.point_count))
        self.view_stats_labels["duration"].setText(f"{stats.duration_s:.6e} s")
        self.view_stats_labels["vpp"].setText(f"{stats.voltage_pp:.6f} V")
        self.view_stats_labels["rms"].setText(f"{stats.voltage_rms:.6f} V")
        self.view_stats_labels["frequency"].setText(_format_optional_hz(stats.estimated_frequency_hz))
        self.view_stats_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.view_stats_labels["duty"].setText(_format_optional_percent(stats.duty_cycle))
        self.view_stats_labels["rise_time"].setText(_format_optional_seconds(stats.rise_time_s))
        self.view_kpi_labels["vpp"].setText(f"{stats.voltage_pp:.4f} V")
        self.view_kpi_labels["frequency"].setText(_format_optional_hz(stats.estimated_frequency_hz))
        self.view_kpi_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.view_kpi_labels["rms"].setText(f"{stats.voltage_rms:.4f} V")

    def _populate_compare_channels(self) -> None:
        self.compare_channel_combo.blockSignals(True)
        self.compare_channel_combo.clear()
        secondary_channels = [waveform.channel for waveform in self.current_waveforms[1:]]
        for channel in secondary_channels:
            self.compare_channel_combo.addItem(_display_channel_name(channel), channel)
        self.compare_channel_combo.blockSignals(False)
        self.compare_channel_combo.setEnabled(bool(secondary_channels))
        self._update_channel_comparison()

    def _update_channel_comparison(self) -> None:
        if len(self.current_waveforms) < 2 or self.current_waveform is None:
            for label in self.compare_labels.values():
                label.setText("-")
            return

        secondary_channel = self.compare_channel_combo.currentData()
        if not secondary_channel:
            for label in self.compare_labels.values():
                label.setText("-")
            return

        secondary_waveform = next((waveform for waveform in self.current_waveforms if waveform.channel == secondary_channel), None)
        if secondary_waveform is None:
            for label in self.compare_labels.values():
                label.setText("-")
            return

        visible_stats = self._primary_visible_stats() or self.current_stats
        frequency_hz = visible_stats.estimated_frequency_hz if visible_stats is not None else None
        edge_type = str(self.compare_edge_combo.currentData())
        comparison = compare_waveform_edges(
            self.current_waveform,
            secondary_waveform,
            self._current_x_focus(),
            edge_type,
            frequency_hz=frequency_hz,
        )
        if comparison is None:
            for label in self.compare_labels.values():
                label.setText("无法估算")
            self.compare_labels["primary_channel"].setText(_display_channel_name(self.current_waveform.channel))
            self.compare_labels["secondary_channel"].setText(_display_channel_name(secondary_waveform.channel))
            self.compare_labels["edge_type"].setText("上升沿" if edge_type == "rising" else "下降沿")
            return

        self.compare_labels["primary_channel"].setText(_display_channel_name(self.current_waveform.channel))
        self.compare_labels["secondary_channel"].setText(_display_channel_name(secondary_waveform.channel))
        self.compare_labels["primary_edge"].setText(f"{comparison.primary_time_s:.6e} s")
        self.compare_labels["secondary_edge"].setText(f"{comparison.secondary_time_s:.6e} s")
        self.compare_labels["delta_t"].setText(f"{comparison.delta_t_s:.6e} s")
        self.compare_labels["phase"].setText(_format_optional_phase(comparison.phase_deg))
        self.compare_labels["frequency"].setText(_format_optional_hz(comparison.frequency_hz))
        self.compare_labels["edge_type"].setText("上升沿" if comparison.edge_type == "rising" else "下降沿")

    def _primary_visible_stats(self) -> WaveformStats | None:
        if self.current_waveform is None:
            return None
        x_axis = self._x_axis()
        if x_axis is None:
            return None
        return self.current_waveform.analyze_window(x_axis.min(), x_axis.max())

    def _render_all_waveform_series(self) -> None:
        for channel in self.waveform_series_map:
            self._render_waveform_series(channel)

    def _render_waveform_series(self, channel: str) -> None:
        series = self.waveform_series_map.get(channel)
        points = self.waveform_decimated_map.get(channel)
        if series is None or points is None:
            return
        series.setPen(self._waveform_series_pen(channel))
        x_values, y_values = points
        offset = self.waveform_offsets.get(channel, 0.0)
        series.clear()
        for x_value, y_value in zip(x_values, y_values):
            series.append(x_value, y_value + offset)

    def _waveform_display_bounds(self) -> tuple[float, float] | None:
        all_y_values: list[float] = []
        for channel, (x_values, y_values) in self.waveform_decimated_map.items():
            offset = self.waveform_offsets.get(channel, 0.0)
            all_y_values.extend(value + offset for value in y_values)
        if not all_y_values:
            return None
        return min(all_y_values), max(all_y_values)

    def _ensure_waveform_offsets_visible(self) -> None:
        axis_y = self._y_axis()
        bounds = self._waveform_display_bounds()
        if axis_y is None or bounds is None:
            return
        display_min, display_max = bounds
        current_min = axis_y.min()
        current_max = axis_y.max()
        if display_min >= current_min and display_max <= current_max:
            return
        span = max(display_max - display_min, 1e-9)
        padding = span * 0.05
        axis_y.setRange(min(display_min - padding, current_min), max(display_max + padding, current_max))

    def _arm_cursor(self, cursor_name: str) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再设置游标。")
            return
        self.pending_cursor_target = cursor_name
        self.cursor_hint_label.setText(f"正在设置游标 {cursor_name.upper()}：请在图上左键单击目标位置。")

    def _handle_chart_click(self, x_value: float, y_value: float, position) -> bool:
        if self.pending_cursor_target is None or self.current_waveform is None:
            return False

        self.cursor_points[self.pending_cursor_target] = self._clamp_value_to_axes((x_value, y_value))
        self.lock_annotation_text = None
        self.pending_cursor_target = None
        self.cursor_hint_label.setText(self._default_cursor_hint())
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()
        return True

    def _handle_chart_drag_start(self, x_value: float, y_value: float, position) -> bool:
        if self.pending_cursor_target is not None:
            return False

        target = self._cursor_drag_target_at(position)
        if target is not None:
            self.dragging_cursor_target = target
            cursor_name, axis_mode = target
            axis_hint = {
                "x": "时间轴",
                "y": "电压轴",
                "xy": "时间轴和电压轴",
            }[axis_mode]
            self.cursor_hint_label.setText(f"正在拖动游标 {cursor_name.upper()}，当前调整 {axis_hint}。")
            return True

        waveform_channel = self._waveform_drag_target_at(position)
        if waveform_channel is None:
            return False
        self.dragging_waveform_channel = waveform_channel
        self.waveform_drag_anchor_y = y_value
        self.waveform_drag_initial_offset = self.waveform_offsets.get(waveform_channel, 0.0)
        self.cursor_hint_label.setText(f"正在拖动 {_display_channel_name(waveform_channel)}，可上下分离显示。")
        self._refresh_cursor_graphics()
        return True

    def _handle_chart_drag_move(self, x_value: float, y_value: float, position) -> bool:
        if self.dragging_cursor_target is None:
            if self.dragging_waveform_channel is None:
                return False
            channel = self.dragging_waveform_channel
            self.waveform_offsets[channel] = self.waveform_drag_initial_offset + (y_value - self.waveform_drag_anchor_y)
            self._render_waveform_series(channel)
            self.cursor_hint_label.setText(
                f"正在拖动 {_display_channel_name(channel)}，显示偏移 {self.waveform_offsets[channel]:+.4f} V。"
            )
            return True

        cursor_name, axis_mode = self.dragging_cursor_target
        point = self.cursor_points.get(cursor_name)
        if point is None:
            return False

        next_x = x_value if "x" in axis_mode else point[0]
        next_y = y_value if "y" in axis_mode else point[1]
        self.cursor_points[cursor_name] = self._clamp_value_to_axes((next_x, next_y))
        self.lock_annotation_text = None
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()
        return True

    def _handle_chart_drag_end(self, x_value: float, y_value: float, position) -> None:
        if self.dragging_cursor_target is not None:
            cursor_name, _ = self.dragging_cursor_target
            self.dragging_cursor_target = None
            self.cursor_hint_label.setText(
                f"游标 {cursor_name.upper()} 已更新。{self._default_cursor_hint()}"
            )
            self._refresh_cursor_graphics()
            return

        if self.dragging_waveform_channel is not None:
            channel = self.dragging_waveform_channel
            self.dragging_waveform_channel = None
            self.cursor_hint_label.setText(
                f"{_display_channel_name(channel)} 已完成分离显示。{self._default_cursor_hint()}"
            )
            self._refresh_cursor_graphics()

    def _clear_cursors(self) -> None:
        self.pending_cursor_target = None
        self.dragging_cursor_target = None
        self.hover_cursor_target = None
        self.cursor_points.clear()
        self.lock_annotation_text = None
        self.cursor_hint_label.setText(self._default_cursor_hint())
        for key in ("a", "b"):
            self.cursor_line_items[key].setVisible(False)
            self.cursor_hline_items[key].setVisible(False)
            self.cursor_handle_items[key].setVisible(False)
            self.cursor_text_items[key].setVisible(False)
            self.cursor_mode_items[key].setVisible(False)
        self.lock_annotation_line.setVisible(False)
        self.lock_annotation_text_item.setVisible(False)
        for label in self.cursor_labels.values():
            label.setText("-")

    def _update_cursor_readouts(self) -> None:
        point_a = self.cursor_points.get("a")
        point_b = self.cursor_points.get("b")
        self.cursor_labels["a"].setText(_format_cursor_point(point_a))
        self.cursor_labels["b"].setText(_format_cursor_point(point_b))

        if point_a is None or point_b is None:
            self.cursor_labels["dt"].setText("-")
            self.cursor_labels["dv"].setText("-")
            self.cursor_labels["slope"].setText("-")
            self.cursor_labels["frequency"].setText("-")
            return

        dt_value = point_b[0] - point_a[0]
        dv_value = point_b[1] - point_a[1]
        self.cursor_labels["dt"].setText(f"{dt_value:.6e} s")
        self.cursor_labels["dv"].setText(f"{dv_value:.6f} V")
        if dt_value == 0:
            self.cursor_labels["slope"].setText("无穷大")
            self.cursor_labels["frequency"].setText("无法估算")
        else:
            self.cursor_labels["slope"].setText(f"{(dv_value / dt_value):.6e} V/s")
            self.cursor_labels["frequency"].setText(f"{(1.0 / abs(dt_value)):.6f} Hz")

    def _snap_cursor_to_edge(self, cursor_name: str, edge_type: str) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再吸附边沿。")
            return

        base_point = self.cursor_points.get(cursor_name)
        x_hint = base_point[0] if base_point is not None else self._current_x_focus()
        snapped_point = self.current_waveform.snap_to_edge(x_hint, edge_type)
        if snapped_point is None:
            edge_label = "上升沿" if edge_type == "rising" else "下降沿"
            self.cursor_hint_label.setText(f"当前波形没有可用的{edge_label}。")
            return

        self.cursor_points[cursor_name] = snapped_point
        self.lock_annotation_text = None
        self.cursor_hint_label.setText(
            f"游标 {cursor_name.upper()} 已吸附到最近{'上升沿' if edge_type == 'rising' else '下降沿'}。"
        )
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _lock_nearest_pulse(self) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再锁定脉冲。")
            return

        pulse = self.current_waveform.find_nearest_pulse(self._current_x_focus())
        if pulse is None:
            self.cursor_hint_label.setText("当前波形没有检测到完整脉冲。")
            return

        self.cursor_points["a"] = pulse.rising_edge
        self.cursor_points["b"] = pulse.falling_edge
        self.lock_annotation_text = "Pulse Window"
        self.cursor_hint_label.setText("已锁定最近完整脉冲，A/B 游标已自动对齐。")
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _lock_nearest_period(self) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再锁定周期。")
            return

        period = self.current_waveform.find_nearest_period(self._current_x_focus(), edge_type="rising")
        if period is None:
            self.cursor_hint_label.setText("当前波形没有检测到完整周期。")
            return

        self.cursor_points["a"] = period.start_edge
        self.cursor_points["b"] = period.end_edge
        self.lock_annotation_text = "Period Window"
        self.cursor_hint_label.setText("已锁定最近完整周期，A/B 游标已对齐到相邻上升沿。")
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _smart_lock_window(self) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再执行智能锁定。")
            return

        recommendation = self.current_waveform.recommend_lock_window(self._current_x_focus())
        if recommendation is None:
            self.cursor_hint_label.setText("当前波形没有检测到可锁定的完整周期或脉冲。")
            return

        self.cursor_points["a"] = recommendation.start_edge
        self.cursor_points["b"] = recommendation.end_edge
        self.lock_annotation_text = "Period Window" if recommendation.mode == "period" else "Pulse Window"
        self.cursor_hint_label.setText(recommendation.description)
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _current_x_focus(self) -> float:
        x_axis = self._x_axis()
        if x_axis is None:
            if self.current_waveform is None or not self.current_waveform.x_values:
                return 0.0
            return (self.current_waveform.x_values[0] + self.current_waveform.x_values[-1]) / 2
        return (x_axis.min() + x_axis.max()) / 2

    def _reset_visual_view(self) -> None:
        self.chart_view.reset_view()
        self.chart_view.reset_horizontal()
        self.chart_view.reset_vertical()
        if any(abs(offset) > 1e-12 for offset in self.waveform_offsets.values()):
            self._clear_waveform_offsets(update_hint=False)
        self._refresh_cursor_graphics()
        self.cursor_hint_label.setText("视图和波形分离偏移已重置。")

    def _x_axis(self) -> QValueAxis | None:
        for axis in self.chart.axes(Qt.Horizontal):
            if isinstance(axis, QValueAxis):
                return axis
        return None

    def _y_axis(self) -> QValueAxis | None:
        for axis in self.chart.axes(Qt.Vertical):
            if isinstance(axis, QValueAxis):
                return axis
        return None

    def _clamp_value_to_axes(self, point: tuple[float, float]) -> tuple[float, float]:
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        x_value, y_value = point
        if x_axis is not None:
            x_value = min(max(x_value, x_axis.min()), x_axis.max())
        if y_axis is not None:
            y_value = min(max(y_value, y_axis.min()), y_axis.max())
        return x_value, y_value

    def _display_point_for_cursor(self, point: tuple[float, float]) -> tuple[float, float]:
        return self._clamp_value_to_axes(point)

    def _refresh_cursor_graphics(self) -> None:
        plot_area = self.chart.plotArea()
        if self.waveform_series is None or self.current_waveform is None or plot_area.isEmpty():
            for key in ("a", "b"):
                self.cursor_line_items[key].setVisible(False)
                self.cursor_hline_items[key].setVisible(False)
                self.cursor_handle_items[key].setVisible(False)
                self.cursor_text_items[key].setVisible(False)
                self.cursor_mode_items[key].setVisible(False)
            self.lock_annotation_line.setVisible(False)
            self.lock_annotation_text_item.setVisible(False)
            return

        for key, color_name in (("a", "A"), ("b", "B")):
            point = self.cursor_points.get(key)
            if point is None:
                self.cursor_line_items[key].setVisible(False)
                self.cursor_hline_items[key].setVisible(False)
                self.cursor_handle_items[key].setVisible(False)
                self.cursor_text_items[key].setVisible(False)
                self.cursor_mode_items[key].setVisible(False)
                continue

            active_mode = None
            if self.dragging_cursor_target is not None and self.dragging_cursor_target[0] == key:
                active_mode = self.dragging_cursor_target[1]
            elif self.hover_cursor_target is not None and self.hover_cursor_target[0] == key:
                active_mode = self.hover_cursor_target[1]
            self.cursor_line_items[key].setPen(self._cursor_pen(key, "x", active_mode))
            self.cursor_hline_items[key].setPen(self._cursor_pen(key, "y", active_mode))
            display_point = self._display_point_for_cursor(point)
            position = self.chart.mapToPosition(QPointF(display_point[0], display_point[1]), self.waveform_series)
            self.cursor_line_items[key].setLine(position.x(), plot_area.top(), position.x(), plot_area.bottom())
            self.cursor_hline_items[key].setLine(plot_area.left(), position.y(), plot_area.right(), position.y())
            handle_radius = 6 if active_mode == "xy" else 5
            self.cursor_handle_items[key].setRect(
                position.x() - handle_radius,
                position.y() - handle_radius,
                handle_radius * 2,
                handle_radius * 2,
            )
            self.cursor_handle_items[key].setPen(self._cursor_pen(key, "xy", active_mode))
            self.cursor_handle_items[key].setBrush(self._cursor_brush(key, active_mode))
            self.cursor_line_items[key].setVisible(True)
            self.cursor_hline_items[key].setVisible(True)
            self.cursor_handle_items[key].setVisible(True)
            self.cursor_text_items[key].setText(f"{color_name}\nt={point[0]:.3e}\nV={point[1]:.3f}")
            label_x = min(position.x() + 6, plot_area.right() - 90)
            label_y = max(min(position.y() - 30, plot_area.bottom() - 48), plot_area.top() + 6)
            if key == "b":
                label_y = min(label_y + 32, plot_area.bottom() - 32)
            self.cursor_text_items[key].setPos(QPointF(label_x, label_y))
            self.cursor_text_items[key].setVisible(True)
            mode_text = self._cursor_mode_text(key, active_mode)
            if mode_text is None:
                self.cursor_mode_items[key].setVisible(False)
            else:
                self.cursor_mode_items[key].setText(mode_text)
                self.cursor_mode_items[key].setPos(QPointF(label_x, max(label_y - 18, plot_area.top() + 2)))
                self.cursor_mode_items[key].setVisible(True)

        point_a = self.cursor_points.get("a")
        point_b = self.cursor_points.get("b")
        if point_a is None or point_b is None or self.lock_annotation_text is None:
            self.lock_annotation_line.setVisible(False)
            self.lock_annotation_text_item.setVisible(False)
            return

        display_point_a = self._display_point_for_cursor(point_a)
        display_point_b = self._display_point_for_cursor(point_b)
        position_a = self.chart.mapToPosition(QPointF(display_point_a[0], display_point_a[1]), self.waveform_series)
        position_b = self.chart.mapToPosition(QPointF(display_point_b[0], display_point_b[1]), self.waveform_series)
        left_x = min(position_a.x(), position_b.x())
        right_x = max(position_a.x(), position_b.x())
        annotation_y = plot_area.bottom() - 18
        self.lock_annotation_line.setLine(left_x, annotation_y, right_x, annotation_y)
        self.lock_annotation_line.setVisible(True)

        delta_t = abs(point_b[0] - point_a[0])
        self.lock_annotation_text_item.setText(f"{self.lock_annotation_text}: {delta_t:.6e} s")
        text_x = min(max((left_x + right_x) / 2 - 70, plot_area.left() + 4), plot_area.right() - 160)
        text_y = annotation_y - 22
        self.lock_annotation_text_item.setPos(QPointF(text_x, text_y))
        self.lock_annotation_text_item.setVisible(True)

    def _cursor_drag_target_at(self, position) -> tuple[str, str] | None:
        if self.waveform_series is None:
            return None

        plot_area = self.chart.plotArea()
        if not plot_area.contains(position):
            return None

        hit_candidates: list[tuple[float, str, str]] = []
        for key in ("a", "b"):
            point = self.cursor_points.get(key)
            if point is None:
                continue
            display_point = self._display_point_for_cursor(point)
            mapped = self.chart.mapToPosition(QPointF(display_point[0], display_point[1]), self.waveform_series)
            dx = abs(position.x() - mapped.x())
            dy = abs(position.y() - mapped.y())
            if dx <= 8 and dy <= 8:
                hit_candidates.append((dx + dy, key, "xy"))
            elif dx <= 6:
                hit_candidates.append((dx, key, "x"))
            elif dy <= 6:
                hit_candidates.append((dy, key, "y"))

        if not hit_candidates:
            return None
        _, key, mode = min(hit_candidates, key=lambda item: item[0])
        return key, mode

    def _waveform_drag_target_at(self, position) -> str | None:
        if len(self.current_waveforms) < 2:
            return None
        plot_area = self.chart.plotArea()
        if plot_area.isEmpty() or not plot_area.contains(position):
            return None

        cursor_value = self.chart.mapToValue(position.toPoint())
        cursor_x = cursor_value.x()
        cursor_y = cursor_value.y()
        hit_candidates: list[tuple[float, str]] = []
        for waveform in self.current_waveforms[1:]:
            channel = waveform.channel
            points = self.waveform_decimated_map.get(channel)
            if points is None:
                continue
            interpolated_y = _interpolate_waveform_y_at_x(points[0], points[1], cursor_x)
            if interpolated_y is None:
                continue
            display_y = interpolated_y + self.waveform_offsets.get(channel, 0.0)
            distance = abs(cursor_y - display_y)
            if distance <= 0.18:
                hit_candidates.append((distance, channel))

        if not hit_candidates:
            return None
        _, channel = min(hit_candidates, key=lambda item: item[0])
        return channel

    def _hover_cursor_shape(self, position) -> Qt.CursorShape:
        target = self.dragging_cursor_target or self._cursor_drag_target_at(position)
        if target != self.hover_cursor_target:
            self.hover_cursor_target = target
            if target is not None:
                self.hover_waveform_channel = None
                if self.pending_cursor_target is None and self.dragging_cursor_target is None:
                    self.cursor_hint_label.setText(f"当前命中 {target[0].upper()}-{target[1].upper()}，可直接拖动。")
                self._refresh_cursor_graphics()
        if target is None:
            waveform_channel = self.dragging_waveform_channel or self._waveform_drag_target_at(position)
            if waveform_channel != self.hover_waveform_channel:
                self.hover_waveform_channel = waveform_channel
                if self.pending_cursor_target is None and self.dragging_cursor_target is None and self.dragging_waveform_channel is None:
                    if waveform_channel is None:
                        self.cursor_hint_label.setText(self._default_cursor_hint())
                    else:
                        self.cursor_hint_label.setText(f"当前命中 {_display_channel_name(waveform_channel)}，可上下拖动分离显示。")
                self._refresh_cursor_graphics()
            if waveform_channel is not None:
                return Qt.SizeVerCursor

        if target is None:
            return Qt.ArrowCursor

        _, mode = target
        if mode == "x":
            return Qt.SizeHorCursor
        if mode == "y":
            return Qt.SizeVerCursor
        return Qt.SizeAllCursor

    def _cursor_pen(self, key: str, axis: str, active_mode: str | None) -> QPen:
        color = QColor("#1f77b4" if key == "a" else "#ff7f0e")
        pen = QPen(color)
        pen.setWidth(4 if active_mode in {axis, "xy"} else 2)
        if active_mode in {axis, "xy"}:
            pen.setColor(color.lighter(115))
        return pen

    def _cursor_brush(self, key: str, active_mode: str | None):
        color = QColor("#1f77b4" if key == "a" else "#ff7f0e")
        fill = QColor(color)
        fill.setAlpha(220 if active_mode == "xy" else 150)
        return fill

    def _cursor_mode_text(self, key: str, active_mode: str | None) -> str | None:
        if active_mode is None:
            return None
        return f"{key.upper()}-{active_mode.upper()}"

    def _default_cursor_hint(self) -> str:
        return "提示：点击“设置游标 A/B”后，在图上单击放置；拖动竖线改时间，拖动横线改电压，拖动交点同时改两者；叠加通道可上下拖动分离显示。"

    def _reset_waveform_offsets(self) -> None:
        self._clear_waveform_offsets(update_hint=True)

    def _clear_waveform_offsets(self, *, update_hint: bool) -> None:
        if not self.waveform_offsets:
            return
        for channel in self.waveform_offsets:
            self.waveform_offsets[channel] = 0.0
        self._render_all_waveform_series()
        self.chart_view.reset_vertical()
        if update_hint:
            self.cursor_hint_label.setText("叠加通道显示偏移已重置。")

    def _waveform_series_pen(self, channel: str) -> QPen:
        channels = [waveform.channel for waveform in self.current_waveforms]
        color_index = channels.index(channel) if channel in channels else 0
        color = QColor(WAVEFORM_SERIES_COLORS[color_index % len(WAVEFORM_SERIES_COLORS)])
        pen = QPen(color)
        is_active = channel == self.dragging_waveform_channel or channel == self.hover_waveform_channel
        pen.setWidth(4 if is_active else 2)
        if is_active:
            pen.setColor(color.lighter(115))
        return pen

    def _export_chart_image(self) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再导出图像。")
            return

        WAVEFORM_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_channel = self.current_waveform.channel.replace(":", "_").replace("/", "_").replace("\\", "_")
        default_path = WAVEFORM_IMAGE_DIR / f"{safe_channel}_{timestamp}.png"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出波形图",
            str(default_path),
            "PNG Files (*.png)",
        )
        if not file_path:
            return

        output_path = Path(file_path)
        image = self.chart_view.grab()
        if image.save(str(output_path), "PNG"):
            self.cursor_hint_label.setText(f"波形图已导出: {output_path}")
        else:
            QMessageBox.critical(self, "导出失败", f"无法保存波形图到 {output_path}")


class WaveformDetailDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("独立波形分析")
        self.resize(1440, 920)
        layout = QVBoxLayout(self)
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
        self.last_startup_brake_result: StartupBrakeTestResult | None = None
        self.startup_brake_history: list[StartupBrakeTestResult] = []
        self.startup_brake_history_timestamps: list[str] = []
        self.startup_brake_history_configs: list[StartupBrakeTestConfig] = []
        self.startup_brake_channel_previous: dict[int, str] = {}
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

        self.startup_brake_dialog = QDialog(self)
        self.startup_brake_dialog.setWindowTitle("启动刹车性能测试")
        self.startup_brake_dialog.resize(1180, 760)
        startup_brake_dialog_layout = QVBoxLayout(self.startup_brake_dialog)
        startup_brake_dialog_layout.addWidget(self._build_startup_brake_test_box())

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
        for combo in self.startup_brake_channel_combos:
            combo.currentIndexChanged.connect(
                lambda _, changed_combo=combo: self._refresh_startup_brake_channel_options(changed_combo)
            )
        self.test_target_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_startup_brake_target_fields())
        self.test_target_value_input.valueChanged.connect(lambda _: self._refresh_startup_brake_target_fields())
        self.test_ppr_input.valueChanged.connect(lambda _: self._refresh_startup_brake_target_fields())
        self.test_brake_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_startup_brake_mode_fields())
        self.run_startup_brake_button.clicked.connect(self.run_startup_brake_test)
        self.apply_startup_cursor_button.clicked.connect(self._apply_startup_cursors)
        self.apply_brake_cursor_button.clicked.connect(self._apply_brake_cursors)
        self.export_startup_stats_button.clicked.connect(self._export_startup_brake_history_csv)
        self.clear_startup_stats_button.clicked.connect(self._clear_startup_brake_history)
        self._refresh_overlay_channel_checks()
        self._stabilize_push_buttons(self)
        self._stabilize_push_buttons(self.startup_brake_dialog)
        self._normalize_label_alignment(self)
        self._normalize_label_alignment(self.startup_brake_dialog)
        self._update_measurement_count()
        self._refresh_auto_measure_button()
        self._clear_startup_brake_results()
        self._refresh_startup_brake_history()
        self._refresh_startup_brake_channel_options()
        self._refresh_startup_brake_target_fields()
        self._refresh_startup_brake_mode_fields()

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

    def _build_startup_brake_test_box(self) -> QGroupBox:
        box = self._group_box("启动刹车性能测试")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        channel_title = QLabel("通道配置")
        channel_title.setFont(QFont(channel_title.font().family(), channel_title.font().pointSize(), QFont.Bold))
        layout.addWidget(channel_title)

        self.test_control_channel_combo = self._create_channel_combo("CHANnel1")
        self.test_speed_channel_combo = self._create_channel_combo("CHANnel2")
        self.test_current_channel_combo = self._create_channel_combo("CHANnel3")
        self.test_encoder_channel_combo = self._create_channel_combo("CHANnel4")
        self.startup_brake_channel_combos = [
            self.test_control_channel_combo,
            self.test_speed_channel_combo,
            self.test_current_channel_combo,
            self.test_encoder_channel_combo,
        ]
        self.startup_brake_channel_previous = {
            id(combo): self._selected_channel_from_combo(combo) for combo in self.startup_brake_channel_combos
        }
        self._set_compact_field_width(
            self.test_control_channel_combo,
            self.test_speed_channel_combo,
            self.test_current_channel_combo,
            self.test_encoder_channel_combo,
        )
        channel_grid = QGridLayout()
        channel_grid.setHorizontalSpacing(12)
        channel_grid.setVerticalSpacing(6)
        channel_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        channel_grid.addWidget(self._inline_form_field("控制输入", self.test_control_channel_combo), 0, 0)
        channel_grid.addWidget(self._inline_form_field("转速反馈", self.test_speed_channel_combo), 0, 1)
        channel_grid.addWidget(self._inline_form_field("电流通道", self.test_current_channel_combo), 1, 0)
        self.test_encoder_field = self._inline_form_field("编码器 A 相", self.test_encoder_channel_combo)
        channel_grid.addWidget(self.test_encoder_field, 1, 1)
        layout.addLayout(channel_grid)

        speed_title = QLabel("达速判定")
        speed_title.setFont(QFont(speed_title.font().family(), speed_title.font().pointSize(), QFont.Bold))
        layout.addWidget(speed_title)

        self.test_target_mode_combo = QComboBox()
        self.test_target_mode_combo.addItem("频率(Hz)", "frequency_hz")
        self.test_target_mode_combo.addItem("周期(ms)", "period_ms")
        self.test_target_mode_combo.addItem("转速(RPM)", "rpm")
        self.test_target_value_input = QDoubleSpinBox()
        self.test_target_value_input.setDecimals(3)
        self.test_target_value_input.setRange(0.001, 1_000_000.0)
        self.test_target_value_input.setValue(100.0)
        self.test_tolerance_input = QDoubleSpinBox()
        self.test_tolerance_input.setDecimals(2)
        self.test_tolerance_input.setSuffix(" %")
        self.test_tolerance_input.setRange(0.0, 100.0)
        self.test_tolerance_input.setValue(5.0)
        self.test_consecutive_input = QDoubleSpinBox()
        self.test_consecutive_input.setDecimals(0)
        self.test_consecutive_input.setRange(1, 20)
        self.test_consecutive_input.setValue(3)
        self.test_ppr_input = QDoubleSpinBox()
        self.test_ppr_input.setDecimals(0)
        self.test_ppr_input.setRange(1, 100000)
        self.test_ppr_input.setValue(1)
        self._set_compact_field_width(
            self.test_target_mode_combo,
            self.test_target_value_input,
            self.test_tolerance_input,
            self.test_consecutive_input,
            self.test_ppr_input,
        )
        speed_grid = QGridLayout()
        speed_grid.setHorizontalSpacing(12)
        speed_grid.setVerticalSpacing(6)
        speed_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        speed_grid.addWidget(self._inline_form_field("目标类型", self.test_target_mode_combo), 0, 0)
        speed_grid.addWidget(self._inline_form_field("目标值", self.test_target_value_input), 0, 1)
        speed_grid.addWidget(self._inline_form_field("容差", self.test_tolerance_input), 0, 2)
        speed_grid.addWidget(self._inline_form_field("连续周期", self.test_consecutive_input), 1, 0)
        self.test_ppr_field = self._inline_form_field("每转脉冲数", self.test_ppr_input)
        speed_grid.addWidget(self.test_ppr_field, 1, 1)
        layout.addLayout(speed_grid)

        self.test_target_hint_label = QLabel("")
        self.test_target_hint_label.setWordWrap(True)
        layout.addWidget(self.test_target_hint_label)

        brake_title = QLabel("刹车判定")
        brake_title.setFont(QFont(brake_title.font().family(), brake_title.font().pointSize(), QFont.Bold))
        layout.addWidget(brake_title)

        self.test_brake_mode_combo = QComboBox()
        self.test_brake_mode_combo.addItem("电流归零", "current_zero")
        self.test_brake_mode_combo.addItem("A相回溯", "encoder_backtrack")
        self.test_zero_threshold_input = QDoubleSpinBox()
        self.test_zero_threshold_input.setDecimals(3)
        self.test_zero_threshold_input.setRange(0.0, 1000.0)
        self.test_zero_threshold_input.setValue(0.05)
        self.test_flat_threshold_input = QDoubleSpinBox()
        self.test_flat_threshold_input.setDecimals(3)
        self.test_flat_threshold_input.setRange(0.0, 1000.0)
        self.test_flat_threshold_input.setValue(0.03)
        self.test_hold_ms_input = QDoubleSpinBox()
        self.test_hold_ms_input.setDecimals(3)
        self.test_hold_ms_input.setSuffix(" ms")
        self.test_hold_ms_input.setRange(0.0, 1000.0)
        self.test_hold_ms_input.setValue(2.0)
        self.test_backtrack_pulses_input = QDoubleSpinBox()
        self.test_backtrack_pulses_input.setDecimals(0)
        self.test_backtrack_pulses_input.setRange(1, 1000)
        self.test_backtrack_pulses_input.setValue(8)
        self._set_compact_field_width(
            self.test_brake_mode_combo,
            self.test_zero_threshold_input,
            self.test_flat_threshold_input,
            self.test_hold_ms_input,
            self.test_backtrack_pulses_input,
        )
        brake_grid = QGridLayout()
        brake_grid.setHorizontalSpacing(12)
        brake_grid.setVerticalSpacing(6)
        brake_grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        brake_grid.addWidget(self._inline_form_field("刹车模式", self.test_brake_mode_combo), 0, 0)
        brake_grid.addWidget(self._inline_form_field("零电流阈值", self.test_zero_threshold_input), 0, 1)
        brake_grid.addWidget(self._inline_form_field("水平线波动", self.test_flat_threshold_input), 0, 2)
        brake_grid.addWidget(self._inline_form_field("保持时间", self.test_hold_ms_input), 1, 0)
        self.test_backtrack_field = self._inline_form_field("回溯脉冲数", self.test_backtrack_pulses_input)
        brake_grid.addWidget(self.test_backtrack_field, 1, 1)
        layout.addLayout(brake_grid)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.run_startup_brake_button = QPushButton("执行测试")
        self.apply_startup_cursor_button = QPushButton("定位启动游标")
        self.apply_brake_cursor_button = QPushButton("定位刹车游标")
        self.export_startup_stats_button = QPushButton("导出统计 CSV")
        self.clear_startup_stats_button = QPushButton("清空统计")
        self.apply_startup_cursor_button.setEnabled(False)
        self.apply_brake_cursor_button.setEnabled(False)
        button_row.addWidget(self.run_startup_brake_button)
        button_row.addSpacing(12)
        button_row.addWidget(self.apply_startup_cursor_button)
        button_row.addWidget(self.apply_brake_cursor_button)
        button_row.addSpacing(12)
        button_row.addWidget(self.export_startup_stats_button)
        button_row.addWidget(self.clear_startup_stats_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        result_stats_row = QHBoxLayout()
        result_stats_row.setSpacing(12)

        single_result_box = self._group_box("单次结果")
        single_result_layout = QVBoxLayout(single_result_box)
        single_result_layout.setContentsMargins(10, 10, 10, 10)
        single_result_layout.setSpacing(8)
        self.startup_brake_result_labels: dict[str, QLabel] = {}
        self.startup_brake_result_cards: dict[str, QWidget] = {}
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
            self.startup_brake_result_labels[key] = value_label
            card = self._metric_card(title, value_label)
            self.startup_brake_result_cards[key] = card
            results_grid.addWidget(card, index // 4, index % 4)
        single_result_layout.addLayout(results_grid)
        result_stats_row.addWidget(single_result_box, 2)

        stats_box = self._group_box("统计范围")
        stats_box_layout = QVBoxLayout(stats_box)
        stats_box_layout.setContentsMargins(10, 10, 10, 10)
        stats_box_layout.setSpacing(8)
        self.startup_brake_stats_labels: dict[str, QLabel] = {}
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
            self.startup_brake_stats_labels[key] = value_label
            stats_grid.addWidget(self._metric_card(title, value_label), index // 2, index % 2)
        stats_box_layout.addLayout(stats_grid)
        result_stats_row.addWidget(stats_box, 1)
        layout.addLayout(result_stats_row)

        history_title = QLabel("测试记录")
        history_title.setFont(QFont(history_title.font().family(), history_title.font().pointSize(), QFont.Bold))
        layout.addWidget(history_title)

        self.startup_brake_history_table = QTableWidget(0, 7)
        self.startup_brake_history_table.setHorizontalHeaderLabels(
            ["#", "时间", "启动(ms)", "刹车(ms)", "启动峰值(A)", "刹车峰值(A)", "命中频率(Hz)"]
        )
        self.startup_brake_history_table.setMinimumHeight(220)
        self.startup_brake_history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.startup_brake_history_table.setSelectionMode(QTableWidget.NoSelection)
        self.startup_brake_history_table.verticalHeader().setVisible(False)
        self.startup_brake_history_table.setAlternatingRowColors(True)
        self.startup_brake_history_table.setShowGrid(False)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.startup_brake_history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.startup_brake_history_table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.startup_brake_history_table.verticalHeader().setDefaultSectionSize(28)
        layout.addWidget(self.startup_brake_history_table, 1)

        self.startup_brake_summary_label = QLabel("提示：执行测试时会优先复用当前波形；缺少通道时会按当前波形采样参数补抓。")
        self.startup_brake_summary_label.setWordWrap(True)
        layout.addWidget(self.startup_brake_summary_label)
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

    def _refresh_startup_brake_channel_options(self, changed_combo: QComboBox | None = None) -> None:
        combos = getattr(self, "startup_brake_channel_combos", [])
        if not combos:
            return

        if changed_combo is None:
            self.startup_brake_channel_previous = {
                id(combo): self._selected_channel_from_combo(combo) for combo in combos
            }
            return

        current_channel = self._selected_channel_from_combo(changed_combo)
        previous_channel = self.startup_brake_channel_previous.get(id(changed_combo), current_channel)
        conflict_combo = next(
            (
                combo
                for combo in combos
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

        self.startup_brake_channel_previous = {
            id(combo): self._selected_channel_from_combo(combo) for combo in combos
        }

    def _refresh_startup_brake_mode_fields(self) -> None:
        brake_mode = str(self.test_brake_mode_combo.currentData())
        encoder_enabled = brake_mode == "encoder_backtrack"
        if hasattr(self, "test_encoder_field"):
            self.test_encoder_field.setEnabled(encoder_enabled)
        if hasattr(self, "test_backtrack_field"):
            self.test_backtrack_field.setEnabled(encoder_enabled)
        self._refresh_startup_brake_result_emphasis(brake_mode)

    def _refresh_startup_brake_target_fields(self) -> None:
        target_mode = str(self.test_target_mode_combo.currentData())
        target_value = float(self.test_target_value_input.value())
        pulses_per_revolution = max(int(self.test_ppr_input.value()), 1)

        ppr_enabled = target_mode == "rpm"
        if hasattr(self, "test_ppr_field"):
            self.test_ppr_field.setEnabled(ppr_enabled)

        if not hasattr(self, "test_target_hint_label"):
            return
        if target_mode == "rpm":
            frequency_hz = (target_value * pulses_per_revolution) / 60.0
            period_ms = (1000.0 / frequency_hz) if frequency_hz > 0 else 0.0
            self.test_target_hint_label.setText(
                f"当前按转速判定：{target_value:.3f} RPM -> {frequency_hz:.6f} Hz -> {period_ms:.6f} ms"
            )
        elif target_mode == "frequency_hz":
            period_ms = (1000.0 / target_value) if target_value > 0 else 0.0
            self.test_target_hint_label.setText(
                f"当前按频率判定：{target_value:.6f} Hz -> {period_ms:.6f} ms"
            )
        elif target_mode == "period_ms":
            frequency_hz = (1000.0 / target_value) if target_value > 0 else 0.0
            self.test_target_hint_label.setText(
                f"当前按周期判定：{target_value:.6f} ms -> {frequency_hz:.6f} Hz"
            )
        else:
            self.test_target_hint_label.setText("")

    def _centered_table_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(int(Qt.AlignCenter))
        return item

    def _refresh_startup_brake_result_emphasis(self, brake_mode: str | None = None) -> None:
        if not hasattr(self, "startup_brake_result_cards"):
            return
        if brake_mode is None:
            brake_mode = str(self.test_brake_mode_combo.currentData())

        muted_keys: set[str]
        if brake_mode == "current_zero":
            muted_keys = {"brake_end"}
        elif brake_mode == "encoder_backtrack":
            muted_keys = {"current_zero"}
        else:
            muted_keys = set()

        for key, card in self.startup_brake_result_cards.items():
            card.setEnabled(key not in muted_keys)

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
        self.last_startup_brake_result = None
        self.export_waveform_button.setEnabled(False)
        self.status_value.setText("未连接")
        self.idn_value.setText("-")
        self.measurement_status.setText("自动测量：未启动")
        self._refresh_auto_measure_button()
        self.waveform_summary.setText("波形状态：尚未抓取")
        self._clear_startup_brake_results()
        self._clear_startup_brake_history()
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
        self.last_startup_brake_result = None
        self.last_waveform_bundle = list(waveforms)
        self.last_waveform_data = primary_waveform
        self.last_waveform_stats = primary_waveform.analyze()
        self._clear_startup_brake_results()
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
        self.startup_brake_dialog.show()
        self.startup_brake_dialog.raise_()
        self.startup_brake_dialog.activateWindow()

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

    def _startup_brake_config_from_ui(self) -> StartupBrakeTestConfig:
        return StartupBrakeTestConfig(
            control_channel=self._selected_channel_from_combo(self.test_control_channel_combo),
            speed_channel=self._selected_channel_from_combo(self.test_speed_channel_combo),
            current_channel=self._selected_channel_from_combo(self.test_current_channel_combo),
            encoder_a_channel=self._selected_channel_from_combo(self.test_encoder_channel_combo),
            speed_target_mode=str(self.test_target_mode_combo.currentData()),
            speed_target_value=float(self.test_target_value_input.value()),
            speed_tolerance_ratio=float(self.test_tolerance_input.value()) / 100.0,
            speed_consecutive_periods=int(self.test_consecutive_input.value()),
            pulses_per_revolution=int(self.test_ppr_input.value()),
            control_threshold_ratio=0.1,
            zero_current_threshold_a=float(self.test_zero_threshold_input.value()),
            zero_current_flat_threshold_a=float(self.test_flat_threshold_input.value()),
            zero_current_hold_s=float(self.test_hold_ms_input.value()) / 1000.0,
            brake_mode=str(self.test_brake_mode_combo.currentData()),
            brake_backtrack_pulses=int(self.test_backtrack_pulses_input.value()),
        )

    def _required_startup_brake_channels(self, config: StartupBrakeTestConfig) -> list[str]:
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

    def run_startup_brake_test(self) -> None:
        config = self._startup_brake_config_from_ui()
        required_channels = self._required_startup_brake_channels(config)
        available_channels = {waveform.channel for waveform in self.last_waveform_bundle}
        if required_channels and set(required_channels).issubset(available_channels):
            self._execute_startup_brake_test(self.last_waveform_bundle, config)
            return

        scope = self.scope
        if scope is None or not scope.is_connected:
            self._show_warning("当前波形缺少测试所需通道，请先连接示波器或加载包含这些通道的波形文件。")
            return

        points_mode = self.waveform_mode_combo.currentText()
        points = int(self.waveform_points_input.value())
        self.startup_brake_summary_label.setText("正在抓取启动刹车测试所需波形...")
        self.log(f"启动刹车测试补抓波形: {', '.join(_display_channel_name(channel) for channel in required_channels)}")
        self._run_task(
            lambda: [scope.fetch_waveform(channel, points_mode=points_mode, points=points) for channel in required_channels],
            on_success=lambda waveforms, captured_config=config: self._on_startup_brake_waveforms_ready(waveforms, captured_config),
            success_message="启动刹车测试波形抓取完成。",
        )

    def _on_startup_brake_waveforms_ready(
        self,
        waveforms: list[WaveformData],
        config: StartupBrakeTestConfig,
    ) -> None:
        self._on_waveforms_fetched(waveforms)
        self._execute_startup_brake_test(waveforms, config)

    def _execute_startup_brake_test(
        self,
        waveforms: list[WaveformData],
        config: StartupBrakeTestConfig,
    ) -> None:
        try:
            result = analyze_startup_brake_test(waveforms, config)
        except Exception as exc:
            self.last_startup_brake_result = None
            self._clear_startup_brake_results(reset_summary=False)
            self.startup_brake_summary_label.setText(f"测试失败：{exc}")
            self.log(f"启动刹车性能测试失败: {exc}")
            self._show_warning(str(exc))
            return

        self.last_startup_brake_result = result
        self.startup_brake_history.append(result)
        self.startup_brake_history_timestamps.append(datetime.now().strftime("%H:%M:%S"))
        self.startup_brake_history_configs.append(config)
        self._update_startup_brake_results(result)
        self._refresh_startup_brake_history()
        self.log(
            "启动刹车性能测试完成: "
            f"启动 {result.startup_delay_s:.6e}s, 刹车 {result.brake_delay_s:.6e}s, "
            f"命中频率 {result.speed_match.frequency_hz:.3f}Hz"
        )

    def _update_startup_brake_results(self, result: StartupBrakeTestResult) -> None:
        labels = self.startup_brake_result_labels
        labels["startup_start"].setText(f"{result.startup_start_point[0]:.6e} s")
        labels["startup_reach"].setText(f"{result.speed_reached_point[0]:.6e} s")
        labels["startup_delay"].setText(f"{result.startup_delay_s:.6e} s")
        labels["startup_peak"].setText(_format_peak_current(result.startup_peak_current))
        labels["startup_peak_time"].setText(_format_peak_time(result.startup_peak_current))
        labels["brake_start"].setText(f"{result.brake_start_point[0]:.6e} s")
        labels["current_zero"].setText(f"{result.current_zero_window.confirmed_time_s:.6e} s")
        labels["brake_end"].setText(f"{result.brake_end_point[0]:.6e} s")
        labels["brake_delay"].setText(f"{result.brake_delay_s:.6e} s")
        labels["brake_peak"].setText(_format_peak_current(result.brake_peak_current))
        labels["speed_frequency"].setText(f"{result.speed_match.frequency_hz:.6f} Hz")
        labels["speed_period"].setText(f"{result.speed_match.period_s * 1000.0:.6f} ms")
        self.apply_startup_cursor_button.setEnabled(True)
        self.apply_brake_cursor_button.setEnabled(True)
        brake_mode_label = "电流归零" if result.brake_mode == "current_zero" else "A相回溯"
        self.startup_brake_summary_label.setText(
            "启动刹车性能测试完成："
            f"第 {len(self.startup_brake_history)} 次样本，"
            f"启动 {result.startup_delay_s:.6e}s，"
            f"刹车 {result.brake_delay_s:.6e}s，"
            f"模式 {brake_mode_label}。"
        )

    def _clear_startup_brake_results(self, *, reset_summary: bool = True) -> None:
        if hasattr(self, "startup_brake_result_labels"):
            for label in self.startup_brake_result_labels.values():
                label.setText("-")
        self._refresh_startup_brake_result_emphasis()
        if hasattr(self, "apply_startup_cursor_button"):
            self.apply_startup_cursor_button.setEnabled(False)
        if hasattr(self, "apply_brake_cursor_button"):
            self.apply_brake_cursor_button.setEnabled(False)
        if reset_summary and hasattr(self, "startup_brake_summary_label"):
            self.startup_brake_summary_label.setText("提示：执行测试时会优先复用当前波形；缺少通道时会按当前波形采样参数补抓。")

    def _clear_startup_brake_history(self) -> None:
        self.startup_brake_history = []
        self.startup_brake_history_timestamps = []
        self.startup_brake_history_configs = []
        self._refresh_startup_brake_history()
        self.startup_brake_summary_label.setText("统计已清空。可继续执行测试重新累计范围。")

    def _export_startup_brake_history_csv(self) -> None:
        if not self.startup_brake_history:
            self._show_warning("当前没有可导出的启动刹车测试统计。")
            return

        STARTUP_BRAKE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = STARTUP_BRAKE_DIR / f"startup_brake_stats_{timestamp}.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self.startup_brake_dialog,
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

            writer.writerow(["summary", "sample_count", str(len(self.startup_brake_history))])
            writer.writerow(["summary", "startup_delay_range_ms", _format_range_ms([result.startup_delay_s * 1000.0 for result in self.startup_brake_history])])
            writer.writerow(["summary", "brake_delay_range_ms", _format_range_ms([result.brake_delay_s * 1000.0 for result in self.startup_brake_history])])
            writer.writerow(["summary", "startup_peak_range_a", _format_range_amp([result.startup_peak_current.value for result in self.startup_brake_history if result.startup_peak_current is not None])])
            writer.writerow(["summary", "brake_peak_range_a", _format_range_amp([result.brake_peak_current.value for result in self.startup_brake_history if result.brake_peak_current is not None])])
            writer.writerow(["summary", "speed_frequency_range_hz", _format_range_hz([result.speed_match.frequency_hz for result in self.startup_brake_history])])
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
            for index, result in enumerate(self.startup_brake_history, start=1):
                config = self.startup_brake_history_configs[index - 1] if index - 1 < len(self.startup_brake_history_configs) else None
                writer.writerow(
                    [
                        index,
                        self.startup_brake_history_timestamps[index - 1] if index - 1 < len(self.startup_brake_history_timestamps) else "-",
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

        self.startup_brake_summary_label.setText(f"统计 CSV 已导出：{output_path}")
        self.log(f"启动刹车统计已导出: {output_path}")

    def _apply_startup_cursors(self) -> None:
        result = self.last_startup_brake_result
        if result is None:
            self._show_warning("请先执行一次启动刹车性能测试。")
            return
        self.waveform_panel.set_cursor_points(
            result.startup_start_point,
            result.speed_reached_point,
            annotation_text="Startup Window",
        )
        if self.waveform_detail_dialog.isVisible():
            self.waveform_detail_dialog.set_cursor_points(
                result.startup_start_point,
                result.speed_reached_point,
                annotation_text="Startup Window",
            )

    def _apply_brake_cursors(self) -> None:
        result = self.last_startup_brake_result
        if result is None:
            self._show_warning("请先执行一次启动刹车性能测试。")
            return
        self.waveform_panel.set_cursor_points(
            result.brake_start_point,
            result.brake_end_point,
            annotation_text="Brake Window",
        )
        if self.waveform_detail_dialog.isVisible():
            self.waveform_detail_dialog.set_cursor_points(
                result.brake_start_point,
                result.brake_end_point,
                annotation_text="Brake Window",
            )

    def _refresh_startup_brake_history(self) -> None:
        if hasattr(self, "startup_brake_history_table"):
            self.startup_brake_history_table.setRowCount(len(self.startup_brake_history))
            for row, result in enumerate(self.startup_brake_history):
                self.startup_brake_history_table.setItem(row, 0, self._centered_table_item(str(row + 1)))
                timestamp = self.startup_brake_history_timestamps[row] if row < len(self.startup_brake_history_timestamps) else "-"
                self.startup_brake_history_table.setItem(row, 1, self._centered_table_item(timestamp))
                self.startup_brake_history_table.setItem(row, 2, self._centered_table_item(f"{result.startup_delay_s * 1000.0:.3f} ms"))
                self.startup_brake_history_table.setItem(row, 3, self._centered_table_item(f"{result.brake_delay_s * 1000.0:.3f} ms"))
                self.startup_brake_history_table.setItem(row, 4, self._centered_table_item(_format_peak_current(result.startup_peak_current)))
                self.startup_brake_history_table.setItem(row, 5, self._centered_table_item(_format_peak_current(result.brake_peak_current)))
                self.startup_brake_history_table.setItem(row, 6, self._centered_table_item(f"{result.speed_match.frequency_hz:.6f} Hz"))

        if not hasattr(self, "startup_brake_stats_labels"):
            return
        if not self.startup_brake_history:
            for label in self.startup_brake_stats_labels.values():
                label.setText("-")
            return

        startup_delays_ms = [result.startup_delay_s * 1000.0 for result in self.startup_brake_history]
        brake_delays_ms = [result.brake_delay_s * 1000.0 for result in self.startup_brake_history]
        startup_peaks = [result.startup_peak_current.value for result in self.startup_brake_history if result.startup_peak_current is not None]
        brake_peaks = [result.brake_peak_current.value for result in self.startup_brake_history if result.brake_peak_current is not None]
        speed_frequencies = [result.speed_match.frequency_hz for result in self.startup_brake_history]

        self.startup_brake_stats_labels["sample_count"].setText(str(len(self.startup_brake_history)))
        self.startup_brake_stats_labels["startup_delay_range"].setText(_format_range_ms(startup_delays_ms))
        self.startup_brake_stats_labels["brake_delay_range"].setText(_format_range_ms(brake_delays_ms))
        self.startup_brake_stats_labels["startup_peak_range"].setText(_format_range_amp(startup_peaks))
        self.startup_brake_stats_labels["brake_peak_range"].setText(_format_range_amp(brake_peaks))
        self.startup_brake_stats_labels["speed_frequency_range"].setText(_format_range_hz(speed_frequencies))

    def _history_config_summary(self, getter) -> str:
        if not self.startup_brake_history_configs:
            return "-"
        values = [getter(config) for config in self.startup_brake_history_configs]
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


def _decimate_xy(x_values: list[float], y_values: list[float], max_points: int) -> tuple[list[float], list[float]]:
    point_count = min(len(x_values), len(y_values))
    if point_count <= max_points:
        return x_values[:point_count], y_values[:point_count]

    step = max(point_count // max_points, 1)
    reduced_x = x_values[::step]
    reduced_y = y_values[::step]
    if reduced_x[-1] != x_values[point_count - 1]:
        reduced_x.append(x_values[point_count - 1])
        reduced_y.append(y_values[point_count - 1])
    return reduced_x, reduced_y


def _interpolate_waveform_y_at_x(x_values: list[float], y_values: list[float], x_value: float) -> float | None:
    point_count = min(len(x_values), len(y_values))
    if point_count == 0:
        return None
    if point_count == 1:
        return y_values[0]
    if x_value < x_values[0] or x_value > x_values[point_count - 1]:
        return None

    low = 0
    high = point_count - 1
    while low < high:
        middle = (low + high) // 2
        if x_values[middle] < x_value:
            low = middle + 1
        else:
            high = middle

    index = low
    if index == 0:
        return y_values[0]
    left_index = index - 1
    right_index = index
    left_x = x_values[left_index]
    right_x = x_values[right_index]
    left_y = y_values[left_index]
    right_y = y_values[right_index]
    if right_x == left_x:
        return right_y
    ratio = (x_value - left_x) / (right_x - left_x)
    return left_y + ratio * (right_y - left_y)

def _format_cursor_point(point: tuple[float, float] | None) -> str:
    if point is None:
        return "-"
    return f"t={point[0]:.6e} s, V={point[1]:.6f}"


def _format_optional_seconds(value: float | None) -> str:
    if value is None:
        return "无法估算"
    return f"{value:.6e} s"


def _format_optional_percent(value: float | None) -> str:
    if value is None:
        return "无法估算"
    return f"{value * 100:.3f} %"


def _format_optional_hz(value: float | None) -> str:
    if value is None:
        return "无法估算"
    return f"{value:.6f} Hz"


def _format_optional_phase(value: float | None) -> str:
    if value is None:
        return "无法估算"
    return f"{value:.3f} deg"


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
