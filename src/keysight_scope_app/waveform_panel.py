from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsSimpleTextItem,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.waveform_analysis import WaveformData, WaveformStats, compare_waveform_edges


WAVEFORM_IMAGE_DIR = Path("captures") / "waveform_images"
WAVEFORM_SERIES_COLORS = ("#2d9cdb", "#eb5757", "#27ae60", "#f2994a")


def _display_channel_name(channel: str) -> str:
    if channel.startswith("CHANnel"):
        return channel.replace("CHANnel", "CH", 1)
    return channel


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
