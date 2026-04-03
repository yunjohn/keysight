from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPoint, QPointF, QRect, QTimer, Qt
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
    QMenu,
    QPushButton,
    QRubberBand,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keysight_scope_app.analysis.waveform import WaveformData, WaveformStats, compare_waveform_edges
from keysight_scope_app.ui.helpers import display_channel_name
from keysight_scope_app.utils import format_engineering_value


WAVEFORM_IMAGE_DIR = Path("captures") / "waveform_images"
WAVEFORM_SERIES_COLORS = ("#2d9cdb", "#eb5757", "#27ae60", "#f2994a")
CURSOR_A_COLOR = "#c2185b"
CURSOR_B_COLOR = "#6a1b9a"
CROSSHAIR_COLOR = "#455a64"
CROSSHAIR_LABEL_COLOR = "#1f2933"
LOCK_ANNOTATION_COLOR = "#264653"
SMART_PREVIEW_COLOR = "#d4a017"
RAW_RENDER_POINT_THRESHOLD = 10000
WAVEFORM_REDRAW_DEBOUNCE_MS = 40


def _should_apply_scope_vertical_layouts(
    channels: list[str],
    unit_resolver,
) -> bool:
    resolved_units = {(unit_resolver(channel) or "V") for channel in channels}
    return len(resolved_units) <= 1


class InteractiveChartView(QChartView):
    def __init__(self, chart: QChart, parent: QWidget | None = None) -> None:
        super().__init__(chart, parent)
        self.point_click_callback = None
        self.hover_cursor_callback = None
        self.hover_leave_callback = None
        self.crosshair_label_callback = None
        self.drag_start_callback = None
        self.drag_move_callback = None
        self.drag_end_callback = None
        self.reset_view_callback = None
        self.default_x_range: tuple[float, float] | None = None
        self.default_y_range: tuple[float, float] | None = None
        self._drag_callback_active = False
        self._selection_origin: QPoint | None = None
        self._selection_active = False
        self._pan_active = False
        self._pan_last_position: QPointF | None = None
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setMouseTracking(True)
        self.setRubberBand(QChartView.NoRubberBand)
        self._selection_band = QRubberBand(QRubberBand.Rectangle, self.viewport())

        pen = QPen(QColor(CROSSHAIR_COLOR))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        self.crosshair_x = QGraphicsLineItem()
        self.crosshair_x.setPen(pen)
        self.crosshair_y = QGraphicsLineItem()
        self.crosshair_y.setPen(pen)
        self.crosshair_label = QGraphicsSimpleTextItem()
        self.crosshair_label.setBrush(QColor(CROSSHAIR_LABEL_COLOR))
        self.crosshair_label.setVisible(False)
        self.chart().scene().addItem(self.crosshair_x)
        self.chart().scene().addItem(self.crosshair_y)
        self.chart().scene().addItem(self.crosshair_label)
        self._hide_crosshair()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self.hover_cursor_callback is not None:
            self.setCursor(self.hover_cursor_callback(event.position()))
        if self._pan_active:
            self._pan_horizontally(event.position())
            self._update_crosshair(event.position())
            event.accept()
            return
        if self._drag_callback_active and self.drag_move_callback is not None:
            value, _ = self._map_position_to_plot_value(event.position())
            if self.drag_move_callback(value.x(), value.y(), event.position()):
                self._update_crosshair(event.position())
                event.accept()
                return
        if self._selection_active:
            self._update_selection_band(event.position())
            self._update_crosshair(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)
        self._update_crosshair(event.position())

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hide_crosshair()
        self.unsetCursor()
        if self.hover_leave_callback is not None:
            self.hover_leave_callback()
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
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.RightButton:
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            position = event.position()
            value, inside_plot = self._map_position_to_plot_value(position)
            if inside_plot and event.modifiers() & Qt.ShiftModifier:
                self._pan_active = True
                self._pan_last_position = QPointF(position)
                event.accept()
                return
            if inside_plot and self.point_click_callback is not None:
                if self.point_click_callback(value.x(), value.y(), position, event.modifiers(), event.button()):
                    event.accept()
                    return
            if self.drag_start_callback is not None:
                if self.drag_start_callback(value.x(), value.y(), position):
                    self._drag_callback_active = True
                    event.accept()
                    return
            if inside_plot:
                self._selection_origin = position.toPoint()
                self._selection_active = True
                self._update_selection_band(position)
                self._selection_band.show()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.RightButton:
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag_callback_active:
            self._drag_callback_active = False
            if self.drag_end_callback is not None:
                value, _ = self._map_position_to_plot_value(event.position())
                self.drag_end_callback(value.x(), value.y(), event.position())
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._pan_active:
            self._pan_active = False
            self._pan_last_position = None
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._selection_active:
            self._finish_selection(event.position())
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

        if self.crosshair_label_callback is not None:
            label_text = self.crosshair_label_callback(value.x(), value.y())
        else:
            label_text = f"t={value.x():.6e} s\nV={value.y():.6f} V"
        self.crosshair_label.setText(label_text)
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

    def _update_selection_band(self, position) -> None:
        if self._selection_origin is None:
            return
        plot_area = self.chart().plotArea()
        left = int(plot_area.left())
        right = int(plot_area.right())
        top = int(plot_area.top())
        bottom = int(plot_area.bottom())
        current_x = int(min(max(position.x(), left), right))
        origin_x = int(min(max(self._selection_origin.x(), left), right))
        current_y = int(min(max(position.y(), top), bottom))
        origin_y = int(min(max(self._selection_origin.y(), top), bottom))
        x_min = min(origin_x, current_x)
        x_max = max(origin_x, current_x)
        y_min = min(origin_y, current_y)
        y_max = max(origin_y, current_y)
        if abs(y_max - y_min) < 6:
            center_y = int((origin_y + current_y) / 2)
            y_min = max(top, center_y - 3)
            y_max = min(bottom, center_y + 3)
        rect = QRect(QPoint(x_min, y_min), QPoint(x_max, y_max))
        self._selection_band.setGeometry(rect.normalized())

    def _finish_selection(self, position) -> None:
        if self._selection_origin is None:
            self._selection_active = False
            self._selection_band.hide()
            return
        plot_area = self.chart().plotArea()
        origin_x = min(max(self._selection_origin.x(), plot_area.left()), plot_area.right())
        current_x = min(max(position.x(), plot_area.left()), plot_area.right())
        self._selection_active = False
        self._selection_origin = None
        self._selection_band.hide()
        if abs(current_x - origin_x) < 4:
            return
        axis_x = self._x_axis()
        if axis_x is None:
            return
        start_value = self.chart().mapToValue(QPoint(int(origin_x), int(plot_area.center().y())))
        end_value = self.chart().mapToValue(QPoint(int(current_x), int(plot_area.center().y())))
        left_value = min(start_value.x(), end_value.x())
        right_value = max(start_value.x(), end_value.x())
        if right_value - left_value <= 0:
            return
        axis_x.setRange(left_value, right_value)

    def _pan_horizontally(self, position) -> None:
        axis_x = self._x_axis()
        if axis_x is None or self._pan_last_position is None:
            return
        plot_area = self.chart().plotArea()
        if plot_area.width() <= 0:
            return
        delta_pixels = position.x() - self._pan_last_position.x()
        if abs(delta_pixels) < 0.5:
            return
        current_min = axis_x.min()
        current_max = axis_x.max()
        span = current_max - current_min
        if span <= 0:
            return
        delta_value = (delta_pixels / plot_area.width()) * span
        axis_x.setRange(current_min - delta_value, current_max - delta_value)
        self._pan_last_position = QPointF(position)


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
        self.active_waveform_channel: str | None = None
        self.scope_vertical_layouts: dict[str, dict[str, float]] = {}
        self.waveform_series: QLineSeries | None = None
        self.waveform_series_map: dict[str, QLineSeries] = {}
        self.waveform_source_map: dict[str, tuple[list[float], list[float]]] = {}
        self.waveform_decimated_map: dict[str, tuple[list[float], list[float]]] = {}
        self.visible_channels: set[str] = set()
        self.waveform_offsets: dict[str, float] = {}
        self.pending_cursor_target: str | None = None
        self.cursor_placement_mode = "direct"
        self.dragging_cursor_target: tuple[str, str] | None = None
        self.hover_cursor_target: tuple[str, str] | None = None
        self.dragging_waveform_channel: str | None = None
        self.hover_waveform_channel: str | None = None
        self.waveform_drag_anchor_y: float = 0.0
        self.waveform_drag_initial_offset: float = 0.0
        self.cursor_points: dict[str, tuple[float, float]] = {}
        self.cursor_channels: dict[str, str | None] = {}
        self.cursor_linked = False
        self.lock_annotation_text: str | None = None
        self.channel_unit_resolver = None
        self.cursor_readout_changed = None
        self.view_window_changed = None
        self._axis_updates_suspended = False
        self._pending_axis_refresh = False
        self._axis_refresh_timer = QTimer(self)
        self._axis_refresh_timer.setSingleShot(True)
        self._axis_refresh_timer.timeout.connect(self._process_axis_range_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.chart_toolbar_widget = QWidget(self)
        chart_toolbar = QHBoxLayout(self.chart_toolbar_widget)
        chart_toolbar.setContentsMargins(0, 0, 0, 0)
        chart_toolbar.setSpacing(10)
        self.reset_view_button = QPushButton("重置视图")
        self.reset_x_button = QPushButton("横向重置")
        self.reset_y_button = QPushButton("纵向重置")
        self.reset_offsets_button = QPushButton("重置分离")
        self.export_image_button = QPushButton("导出图像")
        self.help_label = QLabel(
            "滚轮双轴缩放，Shift+滚轮仅缩放时间轴，Ctrl+滚轮仅缩放电压轴，左键框选仅放大时间轴，右键双击重置。"
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
        layout.addWidget(self.chart_toolbar_widget)

        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.setTitle(title)
        self.chart_view = InteractiveChartView(self.chart)
        self.chart_view.point_click_callback = self._handle_chart_click
        self.chart_view.hover_cursor_callback = self._hover_cursor_shape
        self.chart_view.hover_leave_callback = self._hide_smart_preview
        self.chart_view.crosshair_label_callback = self._crosshair_label_text
        self.chart_view.drag_start_callback = self._handle_chart_drag_start
        self.chart_view.drag_move_callback = self._handle_chart_drag_move
        self.chart_view.drag_end_callback = self._handle_chart_drag_end
        self.chart_view.reset_view_callback = self._reset_visual_view
        self.chart_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.chart_view.customContextMenuRequested.connect(self._show_chart_context_menu)
        self.chart_view.setMinimumHeight(220 if self.compact_mode else 520)
        layout.addWidget(self.chart_view)

        self.cursor_toolbar_widget = QWidget(self)
        cursor_toolbar = QHBoxLayout(self.cursor_toolbar_widget)
        cursor_toolbar.setContentsMargins(0, 0, 0, 0)
        cursor_toolbar.setSpacing(10)
        self.locate_cursor_a_button = QPushButton("定位 A")
        self.locate_cursor_b_button = QPushButton("定位 B")
        self.cursor_mode_combo = QComboBox()
        self.cursor_mode_combo.addItem("点哪放哪", "direct")
        self.cursor_mode_combo.addItem("智能游标", "smart")
        self.cursor_a_rise_button = QPushButton("A 吸附上升沿")
        self.cursor_a_fall_button = QPushButton("A 吸附下降沿")
        self.cursor_b_rise_button = QPushButton("B 吸附上升沿")
        self.cursor_b_fall_button = QPushButton("B 吸附下降沿")
        self.smart_lock_button = QPushButton("智能锁定")
        self.lock_pulse_button = QPushButton("锁定最近脉冲")
        self.lock_period_button = QPushButton("锁定最近周期")
        self.clear_cursor_button = QPushButton("清除游标")
        self.cursor_hint_label = QLabel("提示：在图上右键放置或更新游标；拖动竖线改时间，拖动横线改电压，拖动交点同时改两者。")
        self.cursor_hint_label.setWordWrap(True)
        if not self.compact_mode:
            cursor_toolbar.addWidget(
                self._build_control_group(
                    "游标定位",
                    [
                        self.locate_cursor_a_button,
                        self.locate_cursor_b_button,
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
            layout.addWidget(self.cursor_toolbar_widget)

        self.kpi_title_label = QLabel("当前视图关键指标")
        self.kpi_title_label.setFont(QFont(self.kpi_title_label.font().family(), self.kpi_title_label.font().pointSize(), QFont.Bold))
        self.kpi_title_label.setVisible(not self.compact_mode)
        layout.addWidget(self.kpi_title_label)

        self.kpi_widget = QWidget(self)
        kpi_grid = QGridLayout(self.kpi_widget)
        kpi_grid.setContentsMargins(0, 0, 0, 0)
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
        self.kpi_widget.setVisible(not self.compact_mode)
        layout.addWidget(self.kpi_widget)

        self.stats_tabs = QTabWidget()

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
        self.stats_tabs.addTab(cursor_tab, "游标")

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
        self.stats_tabs.addTab(stats_tab, "全局统计")

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
        self.stats_tabs.addTab(view_stats_tab, "当前视图统计")

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
        self.stats_tabs.addTab(compare_tab, "通道对比")
        if self.compact_mode:
            self.stats_tabs.setCurrentIndex(2)
            self.stats_tabs.setMaximumHeight(210)
        else:
            self.stats_tabs.setMaximumHeight(260)

        self.stats_tabs.setVisible(not self.compact_mode)
        layout.addWidget(self.stats_tabs)
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
        self.locate_cursor_a_button.clicked.connect(lambda: self._locate_cursor("a"))
        self.locate_cursor_b_button.clicked.connect(lambda: self._locate_cursor("b"))
        self.cursor_mode_combo.currentIndexChanged.connect(self._update_cursor_mode)
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

        cursor_pen_a = QPen(QColor(CURSOR_A_COLOR))
        cursor_pen_a.setWidth(2)
        cursor_pen_b = QPen(QColor(CURSOR_B_COLOR))
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
        self.cursor_text_items["a"].setBrush(QColor(CURSOR_A_COLOR))
        self.cursor_text_items["b"].setBrush(QColor(CURSOR_B_COLOR))
        self.cursor_mode_items["a"].setBrush(QColor(CURSOR_A_COLOR))
        self.cursor_mode_items["b"].setBrush(QColor(CURSOR_B_COLOR))
        for key in ("a", "b"):
            self.chart.scene().addItem(self.cursor_line_items[key])
            self.chart.scene().addItem(self.cursor_hline_items[key])
            self.chart.scene().addItem(self.cursor_handle_items[key])
            self.chart.scene().addItem(self.cursor_text_items[key])
            self.chart.scene().addItem(self.cursor_mode_items[key])

        annotation_pen = QPen(QColor(LOCK_ANNOTATION_COLOR))
        annotation_pen.setWidth(2)
        self.lock_annotation_line = QGraphicsLineItem()
        self.lock_annotation_line.setPen(annotation_pen)
        self.lock_annotation_text_item = QGraphicsSimpleTextItem()
        self.lock_annotation_text_item.setBrush(QColor(LOCK_ANNOTATION_COLOR))
        self.chart.scene().addItem(self.lock_annotation_line)
        self.chart.scene().addItem(self.lock_annotation_text_item)
        preview_pen = QPen(QColor(SMART_PREVIEW_COLOR))
        preview_pen.setWidth(2)
        self.smart_preview_item = QGraphicsEllipseItem()
        self.smart_preview_item.setPen(preview_pen)
        preview_fill = QColor(SMART_PREVIEW_COLOR)
        preview_fill.setAlpha(60)
        self.smart_preview_item.setBrush(preview_fill)
        self.smart_preview_label = QGraphicsSimpleTextItem()
        self.smart_preview_label.setBrush(QColor(SMART_PREVIEW_COLOR))
        self.chart.scene().addItem(self.smart_preview_item)
        self.chart.scene().addItem(self.smart_preview_label)
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
        self.active_waveform_channel = self.current_waveform.channel
        self.waveform_series_map = {}
        self.waveform_source_map = {}
        self.waveform_decimated_map = {}
        self.visible_channels = {waveform.channel for waveform in waveforms}
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
        axis_y.setTitleText(self._axis_title_text())
        axis_y.setLabelFormat("%.4g")
        self.chart.addAxis(axis_x, Qt.AlignBottom)
        self.chart.addAxis(axis_y, Qt.AlignLeft)
        axis_x.rangeChanged.connect(self._handle_axis_range_changed)

        all_x_values: list[float] = []
        all_y_values: list[float] = []
        for index, waveform in enumerate(waveforms):
            series = QLineSeries()
            series.setName(display_channel_name(waveform.channel))
            series.setPen(self._waveform_series_pen(waveform.channel))

            self.waveform_source_map[waveform.channel] = (list(waveform.x_values), list(waveform.y_values))
            self.chart.addSeries(series)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
            self.waveform_series_map[waveform.channel] = series
            all_x_values.extend(waveform.x_values)
            all_y_values.extend(waveform.y_values)
            if index == 0:
                self.waveform_series = series

        self._apply_render_quality(waveforms)
        self._axis_updates_suspended = True
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
        self._axis_updates_suspended = False
        self._render_all_waveform_series()
        if self.scope_vertical_layouts:
            self._apply_scope_vertical_layouts()
        self._update_stats(self.current_stats)
        self._update_view_stats_from_axes()
        self._ensure_default_cursors()
        self._refresh_cursor_graphics()

    def capture_view_state(self) -> dict[str, object] | None:
        if not self.current_waveforms:
            return None
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        return {
            "primary_channel": self.current_waveform.channel if self.current_waveform is not None else None,
            "visible_channels": set(self.visible_channels),
            "waveform_offsets": dict(self.waveform_offsets),
            "x_range": (x_axis.min(), x_axis.max()) if x_axis is not None else None,
            "y_range": (y_axis.min(), y_axis.max()) if y_axis is not None else None,
        }

    def restore_view_state(self, state: dict[str, object] | None) -> None:
        if not state or not self.current_waveforms:
            return
        available_channels = {waveform.channel for waveform in self.current_waveforms}
        saved_visible = state.get("visible_channels")
        if isinstance(saved_visible, set):
            self.visible_channels = {channel for channel in saved_visible if channel in available_channels}
            if not self.visible_channels:
                self.visible_channels = {waveform.channel for waveform in self.current_waveforms}

        saved_offsets = state.get("waveform_offsets")
        if isinstance(saved_offsets, dict):
            for channel in available_channels:
                if channel in saved_offsets:
                    try:
                        self.waveform_offsets[channel] = float(saved_offsets[channel])
                    except (TypeError, ValueError):
                        continue

        self._render_all_waveform_series()
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        saved_x_range = state.get("x_range")
        if x_axis is not None and isinstance(saved_x_range, tuple) and len(saved_x_range) == 2:
            x_axis.setRange(float(saved_x_range[0]), float(saved_x_range[1]))
        saved_y_range = state.get("y_range")
        if y_axis is not None and isinstance(saved_y_range, tuple) and len(saved_y_range) == 2:
            y_axis.setRange(float(saved_y_range[0]), float(saved_y_range[1]))
        self._refresh_cursor_graphics()

    def clear(self) -> None:
        self.current_waveforms = []
        self.current_waveform = None
        self.current_stats = None
        self.waveform_series = None
        self.waveform_series_map = {}
        self.waveform_source_map = {}
        self.waveform_decimated_map = {}
        self.visible_channels = set()
        self.active_waveform_channel = None
        self.waveform_offsets = {}
        self.scope_vertical_layouts = {}
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
        self.chart_view.setRenderHint(QPainter.Antialiasing, True)
        self._axis_refresh_timer.stop()
        self._pending_axis_refresh = False
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

    def set_waveform_only_mode(self, enabled: bool) -> None:
        self.chart_toolbar_widget.setVisible(not enabled)
        self.cursor_toolbar_widget.setVisible(not enabled)
        self.kpi_title_label.setVisible(not enabled)
        self.kpi_widget.setVisible(not enabled)
        self.stats_tabs.setVisible(not enabled)
        self.chart_view.setMinimumHeight(680 if enabled else (220 if self.compact_mode else 520))
        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            for index in range(layout.count()):
                layout.setStretch(index, 0)
            layout.setStretch(1, 1 if enabled else 5)
            if not enabled and layout.count() > 5:
                layout.setStretch(5, 1)

    def reset_view(self) -> None:
        self._reset_visual_view()

    def set_smart_cursor_enabled(self, enabled: bool) -> None:
        if self.cursor_placement_mode == "direct":
            return
        self.cursor_placement_mode = "direct"
        combo_index = self.cursor_mode_combo.findData("direct")
        if combo_index >= 0:
            self.cursor_mode_combo.blockSignals(True)
            self.cursor_mode_combo.setCurrentIndex(combo_index)
            self.cursor_mode_combo.blockSignals(False)
        if self.pending_cursor_target is not None:
            self._arm_cursor(self.pending_cursor_target)
        else:
            self.cursor_hint_label.setText(self._default_cursor_hint())
        self._hide_smart_preview()

    def set_cursor_points(
        self,
        point_a: tuple[float, float],
        point_b: tuple[float, float],
        *,
        annotation_text: str | None = None,
        channel_a: str | None = None,
        channel_b: str | None = None,
    ) -> None:
        if self.current_waveform is None:
            return
        self.pending_cursor_target = None
        self.dragging_cursor_target = None
        self.hover_cursor_target = None
        self.cursor_points["a"] = self._clamp_cursor_point(point_a, channel_a)
        self.cursor_points["b"] = self._clamp_cursor_point(point_b, channel_b)
        self.cursor_channels["a"] = channel_a
        self.cursor_channels["b"] = channel_b
        self.lock_annotation_text = annotation_text
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _active_waveform(self) -> WaveformData | None:
        if self.active_waveform_channel is not None:
            for waveform in self.current_waveforms:
                if waveform.channel == self.active_waveform_channel:
                    return waveform
        return self.current_waveform

    def _set_active_waveform_channel(self, channel: str | None) -> None:
        if channel == self.active_waveform_channel:
            return
        self.active_waveform_channel = channel
        self._refresh_waveform_series_styles()
        self._update_view_stats_from_axes()
        self._update_channel_comparison()
        self._refresh_cursor_graphics()

    def _ensure_default_cursors(self) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            return
        if "a" in self.cursor_points and "b" in self.cursor_points:
            return
        if not active_waveform.x_values or not active_waveform.y_values:
            return

        x_values = active_waveform.x_values
        start_x = x_values[0]
        end_x = x_values[-1]
        if end_x <= start_x:
            return
        span = end_x - start_x
        point_a = self._default_cursor_point(start_x + span * 0.25)
        point_b = self._default_cursor_point(start_x + span * 0.75)
        if point_a is None or point_b is None:
            return
        self.set_cursor_points(
            self._display_cursor_point(point_a, active_waveform.channel),
            self._display_cursor_point(point_b, active_waveform.channel),
            channel_a=active_waveform.channel,
            channel_b=active_waveform.channel,
        )

    def _default_cursor_point(self, x_value: float) -> tuple[float, float] | None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            return None
        y_value = _interpolate_waveform_y_at_x(
            active_waveform.x_values,
            active_waveform.y_values,
            x_value,
        )
        if y_value is None:
            return None
        return x_value, y_value

    def _update_stats(self, stats: WaveformStats) -> None:
        unit = self._current_waveform_unit()
        self.stats_labels["point_count"].setText(str(stats.point_count))
        self.stats_labels["duration"].setText(f"{stats.duration_s:.6e} s")
        self.stats_labels["sample_period"].setText(f"{stats.sample_period_s:.6e} s")
        self.stats_labels["vpp"].setText(f"{stats.voltage_pp:.6f} {unit}")
        self.stats_labels["vmin"].setText(f"{stats.voltage_min:.6f} {unit}")
        self.stats_labels["vmax"].setText(f"{stats.voltage_max:.6f} {unit}")
        self.stats_labels["mean"].setText(f"{stats.voltage_mean:.6f} {unit}")
        self.stats_labels["rms"].setText(f"{stats.voltage_rms:.6f} {unit}")
        if stats.estimated_frequency_hz is None:
            self.stats_labels["frequency"].setText("无法估算")
        else:
            self.stats_labels["frequency"].setText(f"{stats.estimated_frequency_hz:.6f} Hz")
        self.stats_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.stats_labels["duty"].setText(_format_optional_percent(stats.duty_cycle))
        self.stats_labels["rise_time"].setText(_format_optional_seconds(stats.rise_time_s))
        self.stats_labels["fall_time"].setText(_format_optional_seconds(stats.fall_time_s))

    def _handle_axis_range_changed(self, minimum: float, maximum: float) -> None:
        if self._axis_updates_suspended:
            return
        self._pending_axis_refresh = True
        self._axis_refresh_timer.start(WAVEFORM_REDRAW_DEBOUNCE_MS)

    def _process_axis_range_changed(self) -> None:
        if not self._pending_axis_refresh:
            return
        self._pending_axis_refresh = False
        self._render_all_waveform_series()
        self._update_view_stats_from_axes()
        self._update_channel_comparison()
        self._refresh_cursor_graphics()

    def _update_view_stats_from_axes(self) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            for label in self.view_stats_labels.values():
                label.setText("-")
            for label in self.view_kpi_labels.values():
                label.setText("-")
            if self.view_window_changed is not None:
                self.view_window_changed()
            return

        x_axis = self._x_axis()
        if x_axis is None:
            for label in self.view_stats_labels.values():
                label.setText("-")
            for label in self.view_kpi_labels.values():
                label.setText("-")
            if self.view_window_changed is not None:
                self.view_window_changed()
            return

        stats = active_waveform.analyze_window(x_axis.min(), x_axis.max())
        if stats is None:
            for label in self.view_stats_labels.values():
                label.setText("不足")
            for label in self.view_kpi_labels.values():
                label.setText("不足")
            if self.view_window_changed is not None:
                self.view_window_changed()
            return

        unit = self._channel_unit(active_waveform.channel)
        self.view_stats_labels["points"].setText(str(stats.point_count))
        self.view_stats_labels["duration"].setText(f"{stats.duration_s:.6e} s")
        self.view_stats_labels["vpp"].setText(f"{stats.voltage_pp:.6f} {unit}")
        self.view_stats_labels["rms"].setText(f"{stats.voltage_rms:.6f} {unit}")
        self.view_stats_labels["frequency"].setText(_format_optional_hz(stats.estimated_frequency_hz))
        self.view_stats_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.view_stats_labels["duty"].setText(_format_optional_percent(stats.duty_cycle))
        self.view_stats_labels["rise_time"].setText(_format_optional_seconds(stats.rise_time_s))
        self.view_kpi_labels["vpp"].setText(f"{stats.voltage_pp:.4f} {unit}")
        self.view_kpi_labels["frequency"].setText(_format_optional_hz(stats.estimated_frequency_hz))
        self.view_kpi_labels["pulse_width"].setText(_format_optional_seconds(stats.pulse_width_s))
        self.view_kpi_labels["rms"].setText(f"{stats.voltage_rms:.4f} {unit}")
        if self.view_window_changed is not None:
            self.view_window_changed()

    def _populate_compare_channels(self) -> None:
        self.compare_channel_combo.blockSignals(True)
        self.compare_channel_combo.clear()
        secondary_channels = [waveform.channel for waveform in self.current_waveforms[1:] if waveform.channel in self.visible_channels]
        for channel in secondary_channels:
            self.compare_channel_combo.addItem(display_channel_name(channel), channel)
        self.compare_channel_combo.blockSignals(False)
        self.compare_channel_combo.setEnabled(bool(secondary_channels))
        self._update_channel_comparison()

    def _update_channel_comparison(self) -> None:
        active_waveform = self._active_waveform()
        if len(self.current_waveforms) < 2 or active_waveform is None:
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
            active_waveform,
            secondary_waveform,
            self._current_x_focus(),
            edge_type,
            frequency_hz=frequency_hz,
        )
        if comparison is None:
            for label in self.compare_labels.values():
                label.setText("无法估算")
            self.compare_labels["primary_channel"].setText(display_channel_name(active_waveform.channel))
            self.compare_labels["secondary_channel"].setText(display_channel_name(secondary_waveform.channel))
            self.compare_labels["edge_type"].setText("上升沿" if edge_type == "rising" else "下降沿")
            return

        self.compare_labels["primary_channel"].setText(display_channel_name(active_waveform.channel))
        self.compare_labels["secondary_channel"].setText(display_channel_name(secondary_waveform.channel))
        self.compare_labels["primary_edge"].setText(f"{comparison.primary_time_s:.6e} s")
        self.compare_labels["secondary_edge"].setText(f"{comparison.secondary_time_s:.6e} s")
        self.compare_labels["delta_t"].setText(f"{comparison.delta_t_s:.6e} s")
        self.compare_labels["phase"].setText(_format_optional_phase(comparison.phase_deg))
        self.compare_labels["frequency"].setText(_format_optional_hz(comparison.frequency_hz))
        self.compare_labels["edge_type"].setText("上升沿" if comparison.edge_type == "rising" else "下降沿")

    def _primary_visible_stats(self) -> WaveformStats | None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            return None
        x_axis = self._x_axis()
        if x_axis is None:
            return None
        return active_waveform.analyze_window(x_axis.min(), x_axis.max())

    def visible_stats_for_channel(self, channel: str) -> WaveformStats | None:
        waveform = next((item for item in self.current_waveforms if item.channel == channel), None)
        if waveform is None:
            return None
        x_axis = self._x_axis()
        if x_axis is None:
            return None
        return waveform.analyze_window(x_axis.min(), x_axis.max())

    def full_stats_for_channel(self, channel: str) -> WaveformStats | None:
        waveform = next((item for item in self.current_waveforms if item.channel == channel), None)
        if waveform is None:
            return None
        return waveform.analyze()

    def cursor_time_window(self) -> tuple[float, float] | None:
        point_a = self.cursor_points.get("a")
        point_b = self.cursor_points.get("b")
        if point_a is None or point_b is None:
            return None
        if point_a[0] == point_b[0]:
            return None
        return (min(point_a[0], point_b[0]), max(point_a[0], point_b[0]))

    def cursor_window_stats_for_channel(self, channel: str) -> WaveformStats | None:
        waveform = next((item for item in self.current_waveforms if item.channel == channel), None)
        if waveform is None:
            return None
        time_window = self.cursor_time_window()
        if time_window is None:
            return None
        return waveform.analyze_window(*time_window)

    def _render_all_waveform_series(self) -> None:
        for channel in self.waveform_series_map:
            self._render_waveform_series(channel)
        self.chart.legend().setVisible(len(self.visible_channels) > 1)
        self._refresh_chart_title()

    def _refresh_waveform_series_styles(self) -> None:
        for channel, series in self.waveform_series_map.items():
            series.setPen(self._waveform_series_pen(channel))
        self.chart.legend().setVisible(len(self.visible_channels) > 1)
        self._refresh_chart_title()

    def _render_waveform_series(self, channel: str) -> None:
        series = self.waveform_series_map.get(channel)
        points = self._visible_waveform_points(channel)
        if series is None or points is None:
            return
        if channel not in self.visible_channels:
            series.clear()
            series.setVisible(False)
            self.waveform_decimated_map.pop(channel, None)
            return
        self.waveform_decimated_map[channel] = points
        series.setPen(self._waveform_series_pen(channel))
        x_values, y_values = points
        offset = self.waveform_offsets.get(channel, 0.0)
        series.setVisible(True)
        series.replace([QPointF(x_value, y_value + offset) for x_value, y_value in zip(x_values, y_values)])

    def _waveform_display_bounds(self) -> tuple[float, float] | None:
        all_y_values: list[float] = []
        for channel, (x_values, y_values) in self.waveform_decimated_map.items():
            if channel not in self.visible_channels:
                continue
            offset = self.waveform_offsets.get(channel, 0.0)
            all_y_values.extend(value + offset for value in y_values)
        if not all_y_values:
            return None
        return min(all_y_values), max(all_y_values)

    def set_visible_channels(self, channels: set[str]) -> None:
        available_channels = {waveform.channel for waveform in self.current_waveforms}
        self.visible_channels = {channel for channel in channels if channel in available_channels}
        if self.active_waveform_channel not in self.visible_channels:
            self.active_waveform_channel = next(iter(self.visible_channels), None)
        self._populate_compare_channels()
        if self.scope_vertical_layouts:
            self._apply_scope_vertical_layouts()
        else:
            self._render_all_waveform_series()
        self._refresh_cursor_graphics()

    def _refresh_chart_title(self) -> None:
        if not self.current_waveforms:
            self.chart.setTitle("尚未加载波形")
            return
        axis_y = self._y_axis()
        if axis_y is not None:
            axis_y.setTitleText(self._axis_title_text())
        visible_labels = []
        for waveform in self.current_waveforms:
            if waveform.channel not in self.visible_channels:
                continue
            label = display_channel_name(waveform.channel)
            if waveform.channel == self.active_waveform_channel:
                label = f"[{label}]"
            visible_labels.append(label)
        if visible_labels:
            self.chart.setTitle(" / ".join(visible_labels) + " 波形")
        else:
            self.chart.setTitle("已隐藏全部通道")

    def _channel_unit(self, channel: str) -> str:
        if self.channel_unit_resolver is None:
            return "V"
        return str(self.channel_unit_resolver(channel) or "V")

    def _current_waveform_unit(self) -> str:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            return "V"
        return self._channel_unit(active_waveform.channel)

    def _axis_title_text(self) -> str:
        return "Current (A)" if self._current_waveform_unit() == "A" else "Voltage (V)"

    def _format_cursor_point(self, point: tuple[float, float] | None, unit: str) -> str:
        if point is None:
            return "-"
        return f"t={_format_time_value(point[0])}, Y={point[1]:.6f} {unit}"

    def _visible_waveform_points(self, channel: str) -> tuple[list[float], list[float]] | None:
        points = self.waveform_source_map.get(channel)
        if points is None:
            return None

        x_values, y_values = points
        x_axis = self._x_axis()
        if x_axis is None:
            return _decimate_xy_envelope(x_values, y_values, max_points=2500)

        left, right = x_axis.min(), x_axis.max()
        visible_x, visible_y = _slice_xy_by_range(x_values, y_values, left, right)
        if not visible_x:
            return _decimate_xy_envelope(x_values, y_values, max_points=2500)

        plot_width = max(int(self.chart.plotArea().width()), 1)
        max_points = max(plot_width * 2, 1200)
        return _decimate_xy_envelope(visible_x, visible_y, max_points=max_points)

    def _apply_render_quality(self, waveforms: list[WaveformData]) -> None:
        should_disable_antialias = any(
            waveform.points_mode.upper() == "RAW" and len(waveform.x_values) >= RAW_RENDER_POINT_THRESHOLD
            for waveform in waveforms
        )
        self.chart_view.setRenderHint(QPainter.Antialiasing, not should_disable_antialias)

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

    def _apply_scope_vertical_layouts(self) -> None:
        if not self.current_waveforms or not self.scope_vertical_layouts:
            return
        target_channels = [waveform.channel for waveform in self.current_waveforms if waveform.channel in self.visible_channels]
        if not target_channels:
            target_channels = [waveform.channel for waveform in self.current_waveforms]
        if not _should_apply_scope_vertical_layouts(target_channels, self._channel_unit):
            for channel in self.waveform_offsets:
                self.waveform_offsets[channel] = 0.0
            self._render_all_waveform_series()
            self._ensure_waveform_offsets_visible()
            return
        primary_channel = self.current_waveform.channel if self.current_waveform is not None else self.current_waveforms[0].channel
        primary_layout = self.scope_vertical_layouts.get(primary_channel)
        if primary_layout is None:
            return
        base_scale = float(primary_layout.get("scale", 0.0) or 1.0)
        primary_divisions = -float(primary_layout.get("offset", 0.0)) / base_scale
        for waveform in self.current_waveforms:
            channel = waveform.channel
            layout = self.scope_vertical_layouts.get(channel)
            if layout is None:
                self.waveform_offsets[channel] = 0.0
                continue
            channel_scale = float(layout.get("scale", 0.0) or 1.0)
            channel_divisions = -float(layout.get("offset", 0.0)) / channel_scale
            self.waveform_offsets[channel] = (channel_divisions - primary_divisions) * base_scale
        self._render_all_waveform_series()
        self._ensure_waveform_offsets_visible()

    def _arm_cursor(self, cursor_name: str) -> None:
        if self.current_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再设置游标。")
            return
        self.pending_cursor_target = cursor_name
        self.cursor_hint_label.setText(f"请在图上右键菜单中选择“设置/更新游标 {cursor_name.upper()}”。")

    def _handle_chart_click(self, x_value: float, y_value: float, position, modifiers, button) -> bool:
        if self.current_waveform is None:
            return False
        target_waveform = self._waveform_for_cursor_position(x_value, y_value, position)
        target_channel = target_waveform.channel if target_waveform is not None else None
        if target_channel is not None:
            self._set_active_waveform_channel(target_channel)
        return False

    def _cursor_to_place_or_update(self, x_value: float, y_value: float) -> str:
        point_a = self.cursor_points.get("a")
        point_b = self.cursor_points.get("b")
        if point_a is None:
            return "a"
        if point_b is None:
            return "b"
        distance_a = abs(point_a[0] - x_value) + abs(point_a[1] - y_value)
        distance_b = abs(point_b[0] - x_value) + abs(point_b[1] - y_value)
        return "a" if distance_a <= distance_b else "b"

    def _show_chart_context_menu(self, position) -> None:
        if self.current_waveform is None:
            return
        plot_position = QPointF(position)
        plot_area = self.chart.plotArea()
        if not plot_area.contains(plot_position):
            return

        mapped = self.chart.mapToValue(plot_position.toPoint())
        target_waveform = self._waveform_for_cursor_position(mapped.x(), mapped.y(), plot_position)
        target_channel = target_waveform.channel if target_waveform is not None else None
        if target_channel is not None:
            self._set_active_waveform_channel(target_channel)

        menu = QMenu(self)
        action_a = menu.addAction("设置/更新游标 A")
        action_b = menu.addAction("设置/更新游标 B")
        menu.addSeparator()
        link_action = menu.addAction("游标联动拖动")
        link_action.setCheckable(True)
        link_action.setChecked(self.cursor_linked)
        menu.addSeparator()
        locate_a = menu.addAction("定位 A")
        locate_b = menu.addAction("定位 B")
        menu.addSeparator()
        clear_action = menu.addAction("清除游标")

        selected = menu.exec(self.chart_view.mapToGlobal(position))
        if selected == action_a:
            self._place_cursor_at("a", mapped.x(), mapped.y(), target_channel)
        elif selected == action_b:
            self._place_cursor_at("b", mapped.x(), mapped.y(), target_channel)
        elif selected == link_action:
            self.cursor_linked = link_action.isChecked()
            mode_text = "已开启" if self.cursor_linked else "已关闭"
            self.cursor_hint_label.setText(f"游标联动拖动{mode_text}。{self._default_cursor_hint()}")
        elif selected == locate_a:
            self._locate_cursor("a")
        elif selected == locate_b:
            self._locate_cursor("b")
        elif selected == clear_action:
            self._clear_cursors()

    def _place_cursor_at(self, cursor_name: str, x_value: float, y_value: float, channel: str | None) -> None:
        self.cursor_points[cursor_name] = self._clamp_cursor_point((x_value, y_value), channel)
        self.cursor_channels[cursor_name] = channel
        self.lock_annotation_text = None
        self.pending_cursor_target = None
        self.cursor_hint_label.setText(f"已设置游标 {cursor_name.upper()}。{self._default_cursor_hint()}")
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

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
        self.cursor_hint_label.setText(f"正在拖动 {display_channel_name(waveform_channel)}，可上下分离显示。")
        self._refresh_cursor_graphics()
        return True

    def _handle_chart_drag_move(self, x_value: float, y_value: float, position) -> bool:
        if self.dragging_cursor_target is None:
            if self.dragging_waveform_channel is None:
                return False
            channel = self.dragging_waveform_channel
            unit = self._channel_unit(channel)
            self.waveform_offsets[channel] = self.waveform_drag_initial_offset + (y_value - self.waveform_drag_anchor_y)
            self._render_waveform_series(channel)
            self.cursor_hint_label.setText(
                f"正在拖动 {display_channel_name(channel)}，显示偏移 {self.waveform_offsets[channel]:+.4f} {unit}。"
            )
            return True

        cursor_name, axis_mode = self.dragging_cursor_target
        point = self.cursor_points.get(cursor_name)
        if point is None:
            return False

        original_point = point
        next_x = x_value if "x" in axis_mode else point[0]
        next_y = y_value if "y" in axis_mode else point[1]
        self.cursor_points[cursor_name] = self._clamp_cursor_point(
            (next_x, next_y),
            self.cursor_channels.get(cursor_name),
        )
        if self.cursor_linked:
            other_cursor_name = "b" if cursor_name == "a" else "a"
            other_point = self.cursor_points.get(other_cursor_name)
            if other_point is not None:
                delta_x = self.cursor_points[cursor_name][0] - original_point[0]
                delta_y = self.cursor_points[cursor_name][1] - original_point[1]
                self.cursor_points[other_cursor_name] = self._clamp_cursor_point(
                    (other_point[0] + delta_x, other_point[1] + delta_y),
                    self.cursor_channels.get(other_cursor_name),
                )
        self._keep_cursor_point_visible(self.cursor_points[cursor_name])
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
                f"{display_channel_name(channel)} 已完成分离显示。{self._default_cursor_hint()}"
            )
            self._refresh_cursor_graphics()

    def _locate_cursor(self, cursor_name: str) -> None:
        point = self.cursor_points.get(cursor_name)
        if point is None:
            self.cursor_hint_label.setText(f"游标 {cursor_name.upper()} 尚未放置。")
            return
        channel = self.cursor_channels.get(cursor_name)
        raw_point = self._raw_cursor_point(point, channel)
        if raw_point is None:
            self.cursor_hint_label.setText(f"游标 {cursor_name.upper()} 尚未放置。")
            return
        self._center_view_on_point(raw_point, channel=channel)
        self._refresh_cursor_graphics()
        self.cursor_hint_label.setText(f"已定位到游标 {cursor_name.upper()}。")

    def _clear_cursors(self) -> None:
        self.pending_cursor_target = None
        self.dragging_cursor_target = None
        self.hover_cursor_target = None
        self.cursor_points.clear()
        self.cursor_channels.clear()
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
        self.smart_preview_item.setVisible(False)
        self.smart_preview_label.setVisible(False)
        for label in self.cursor_labels.values():
            label.setText("-")
        self._notify_cursor_readout_changed()

    def _update_cursor_readouts(self) -> None:
        point_a = self.cursor_points.get("a")
        point_b = self.cursor_points.get("b")
        raw_point_a = self._raw_cursor_point(point_a, self.cursor_channels.get("a"))
        raw_point_b = self._raw_cursor_point(point_b, self.cursor_channels.get("b"))
        unit_a = self._channel_unit(self.cursor_channels.get("a")) if self.cursor_channels.get("a") is not None else self._current_waveform_unit()
        unit_b = self._channel_unit(self.cursor_channels.get("b")) if self.cursor_channels.get("b") is not None else self._current_waveform_unit()
        self.cursor_labels["a"].setText(self._format_cursor_point(raw_point_a, unit_a))
        self.cursor_labels["b"].setText(self._format_cursor_point(raw_point_b, unit_b))
        self._update_cursor_visibility_hint()

        if raw_point_a is None or raw_point_b is None:
            self.cursor_labels["dt"].setText("-")
            self.cursor_labels["dv"].setText("-")
            self.cursor_labels["slope"].setText("-")
            self.cursor_labels["frequency"].setText("-")
            self._notify_cursor_readout_changed()
            return

        dt_value = raw_point_b[0] - raw_point_a[0]
        dv_value = raw_point_b[1] - raw_point_a[1]
        self.cursor_labels["dt"].setText(_format_time_value(dt_value))
        dv_unit = unit_a if unit_a == unit_b else f"{unit_a}/{unit_b}"
        self.cursor_labels["dv"].setText(f"{dv_value:.6f} {dv_unit}")
        if dt_value == 0:
            self.cursor_labels["slope"].setText("无穷大")
            self.cursor_labels["frequency"].setText("无法估算")
        else:
            self.cursor_labels["slope"].setText(f"{(dv_value / dt_value):.6e} {dv_unit}/s")
            self.cursor_labels["frequency"].setText(f"{(1.0 / abs(dt_value)):.6f} Hz")
        self._notify_cursor_readout_changed()

    def get_cursor_measurements(self) -> dict[str, str]:
        return {
            "游标 A": self.cursor_labels["a"].text(),
            "游标 B": self.cursor_labels["b"].text(),
            "Δt": self.cursor_labels["dt"].text(),
            "ΔV/ΔI": self.cursor_labels["dv"].text(),
            "ΔV/Δt": self.cursor_labels["slope"].text(),
            "1/Δt": self.cursor_labels["frequency"].text(),
        }

    def _notify_cursor_readout_changed(self) -> None:
        if self.cursor_readout_changed is not None:
            self.cursor_readout_changed(self.get_cursor_measurements())

    def _update_cursor_visibility_hint(self) -> None:
        if self.pending_cursor_target is not None or self.dragging_cursor_target is not None:
            return
        invisible: list[str] = []
        for cursor_name in ("a", "b"):
            point = self.cursor_points.get(cursor_name)
            if point is None:
                continue
            if not self._point_visible_in_axes(point):
                invisible.append(cursor_name.upper())
        if not invisible:
            return
        joined = " / ".join(invisible)
        self.cursor_hint_label.setText(f"游标 {joined} 在当前视图外，可点“定位 A/B”跳回，或直接重新设置。")

    def _update_cursor_mode(self, index: int) -> None:
        self.cursor_placement_mode = "direct"
        if self.pending_cursor_target is not None:
            self._arm_cursor(self.pending_cursor_target)

    def _nearest_smart_point(
        self,
        x_hint: float,
        waveform: WaveformData | None = None,
    ) -> tuple[float, float] | None:
        target_waveform = waveform or self._active_waveform()
        if target_waveform is None:
            return None

        candidates: list[tuple[float, float]] = []
        for edge_type in ("rising", "falling"):
            edge = target_waveform.snap_to_edge(x_hint, edge_type)
            if edge is not None:
                candidates.append(edge)

        pulse = target_waveform.find_nearest_pulse(x_hint)
        if pulse is not None:
            candidates.extend([pulse.rising_edge, pulse.falling_edge])

        period = target_waveform.find_nearest_period(x_hint, edge_type="rising")
        if period is not None:
            candidates.extend([period.start_edge, period.end_edge])

        sample_point = self._nearest_sample_extreme(x_hint, target_waveform)
        if sample_point is not None:
            candidates.append(sample_point)

        if not candidates:
            return None
        return min(candidates, key=lambda point: abs(point[0] - x_hint))

    def _nearest_sample_extreme(
        self,
        x_hint: float,
        waveform: WaveformData | None = None,
    ) -> tuple[float, float] | None:
        target_waveform = waveform or self._active_waveform()
        if target_waveform is None or not target_waveform.x_values:
            return None

        x_values = target_waveform.x_values
        y_values = target_waveform.y_values
        point_count = min(len(x_values), len(y_values))
        nearest_index = min(range(point_count), key=lambda index: abs(x_values[index] - x_hint))
        left = max(0, nearest_index - 20)
        right = min(point_count, nearest_index + 21)
        window = list(zip(x_values[left:right], y_values[left:right]))
        if not window:
            return None
        peak = max(window, key=lambda item: abs(item[1]))
        return peak

    def _smart_place_pulse_window(self, x_hint: float, waveform: WaveformData | None = None) -> bool:
        target_waveform = waveform or self._active_waveform()
        if target_waveform is None:
            return False
        pulse = target_waveform.find_nearest_pulse(x_hint)
        if pulse is None:
            self.cursor_hint_label.setText("当前附近没有完整脉冲可供智能放置。")
            return False
        channel = target_waveform.channel
        self.cursor_points["a"] = self._clamp_cursor_point(self._display_cursor_point(pulse.rising_edge, channel), channel)
        self.cursor_points["b"] = self._clamp_cursor_point(self._display_cursor_point(pulse.falling_edge, channel), channel)
        self.cursor_channels["a"] = channel
        self.cursor_channels["b"] = channel
        self.pending_cursor_target = None
        self.lock_annotation_text = "Pulse Window"
        self.cursor_hint_label.setText("已按最近脉冲自动放置 A/B 游标。")
        self._hide_smart_preview()
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()
        return True

    def _smart_place_period_window(self, x_hint: float, waveform: WaveformData | None = None) -> bool:
        target_waveform = waveform or self._active_waveform()
        if target_waveform is None:
            return False
        period = target_waveform.find_nearest_period(x_hint, edge_type="rising")
        if period is None:
            self.cursor_hint_label.setText("当前附近没有完整周期可供智能放置。")
            return False
        channel = target_waveform.channel
        self.cursor_points["a"] = self._clamp_cursor_point(self._display_cursor_point(period.start_edge, channel), channel)
        self.cursor_points["b"] = self._clamp_cursor_point(self._display_cursor_point(period.end_edge, channel), channel)
        self.cursor_channels["a"] = channel
        self.cursor_channels["b"] = channel
        self.pending_cursor_target = None
        self.lock_annotation_text = "Period Window"
        self.cursor_hint_label.setText("已按最近周期自动放置 A/B 游标。")
        self._hide_smart_preview()
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()
        return True

    def _snap_cursor_to_edge(self, cursor_name: str, edge_type: str) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再吸附边沿。")
            return

        base_point = self.cursor_points.get(cursor_name)
        x_hint = base_point[0] if base_point is not None else self._current_x_focus()
        snapped_point = active_waveform.snap_to_edge(x_hint, edge_type)
        if snapped_point is None:
            edge_label = "上升沿" if edge_type == "rising" else "下降沿"
            self.cursor_hint_label.setText(f"当前波形没有可用的{edge_label}。")
            return

        channel = active_waveform.channel
        self.cursor_points[cursor_name] = self._clamp_cursor_point(self._display_cursor_point(snapped_point, channel), channel)
        self.cursor_channels[cursor_name] = channel
        self.lock_annotation_text = None
        self.cursor_hint_label.setText(
            f"游标 {cursor_name.upper()} 已吸附到最近{'上升沿' if edge_type == 'rising' else '下降沿'}。"
        )
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _lock_nearest_pulse(self) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再锁定脉冲。")
            return

        pulse = active_waveform.find_nearest_pulse(self._current_x_focus())
        if pulse is None:
            self.cursor_hint_label.setText("当前波形没有检测到完整脉冲。")
            return

        channel = active_waveform.channel
        self.cursor_points["a"] = self._clamp_cursor_point(self._display_cursor_point(pulse.rising_edge, channel), channel)
        self.cursor_points["b"] = self._clamp_cursor_point(self._display_cursor_point(pulse.falling_edge, channel), channel)
        self.cursor_channels["a"] = channel
        self.cursor_channels["b"] = channel
        self.lock_annotation_text = "Pulse Window"
        self.cursor_hint_label.setText("已锁定最近完整脉冲，A/B 游标已自动对齐。")
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _lock_nearest_period(self) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再锁定周期。")
            return

        period = active_waveform.find_nearest_period(self._current_x_focus(), edge_type="rising")
        if period is None:
            self.cursor_hint_label.setText("当前波形没有检测到完整周期。")
            return

        channel = active_waveform.channel
        self.cursor_points["a"] = self._clamp_cursor_point(self._display_cursor_point(period.start_edge, channel), channel)
        self.cursor_points["b"] = self._clamp_cursor_point(self._display_cursor_point(period.end_edge, channel), channel)
        self.cursor_channels["a"] = channel
        self.cursor_channels["b"] = channel
        self.lock_annotation_text = "Period Window"
        self.cursor_hint_label.setText("已锁定最近完整周期，A/B 游标已对齐到相邻上升沿。")
        self._update_cursor_readouts()
        self._refresh_cursor_graphics()

    def _smart_lock_window(self) -> None:
        active_waveform = self._active_waveform()
        if active_waveform is None:
            self.cursor_hint_label.setText("请先抓取或加载波形，再执行智能锁定。")
            return

        recommendation = active_waveform.recommend_lock_window(self._current_x_focus())
        if recommendation is None:
            self.cursor_hint_label.setText("当前波形没有检测到可锁定的完整周期或脉冲。")
            return

        channel = active_waveform.channel
        self.cursor_points["a"] = self._clamp_cursor_point(self._display_cursor_point(recommendation.start_edge, channel), channel)
        self.cursor_points["b"] = self._clamp_cursor_point(self._display_cursor_point(recommendation.end_edge, channel), channel)
        self.cursor_channels["a"] = channel
        self.cursor_channels["b"] = channel
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

    def set_timebase_scale(self, seconds_per_div: float, *, divisions: int = 10) -> None:
        if self.current_waveform is None or not self.current_waveform.x_values:
            return
        if seconds_per_div <= 0 or divisions <= 0:
            return

        x_axis = self._x_axis()
        if x_axis is None:
            return

        full_start = self.current_waveform.x_values[0]
        full_end = self.current_waveform.x_values[-1]
        span = seconds_per_div * divisions
        if full_end <= full_start:
            return
        if span >= (full_end - full_start):
            x_axis.setRange(full_start, full_end)
            return

        target_end = min(full_start + span, full_end)
        x_axis.setRange(full_start, target_end)

    def set_scope_vertical_layouts(self, layouts: dict[str, dict[str, float]]) -> None:
        self.scope_vertical_layouts = dict(layouts)
        self._apply_scope_vertical_layouts()

    def focus_on_point(self, point: tuple[float, float], *, annotation_text: str | None = None) -> None:
        self.focus_on_channel_point(point, channel=None, annotation_text=annotation_text)

    def focus_on_channel_point(
        self,
        point: tuple[float, float],
        *,
        channel: str | None,
        annotation_text: str | None = None,
    ) -> None:
        self._center_view_on_point(point, channel=channel)
        display_point = self._display_cursor_point(point, channel)
        self.set_cursor_points(
            display_point,
            display_point,
            annotation_text=annotation_text,
            channel_a=channel,
            channel_b=channel,
        )

    def _center_view_on_point(
        self,
        point: tuple[float, float],
        *,
        channel: str | None,
    ) -> None:
        if self.current_waveform is None or not self.current_waveform.x_values:
            return
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        display_point = self._display_cursor_point(point, channel)
        if x_axis is not None:
            full_start = self.current_waveform.x_values[0]
            full_end = self.current_waveform.x_values[-1]
            current_span = max(x_axis.max() - x_axis.min(), 1e-9)
            half_span = current_span / 2
            next_min = max(point[0] - half_span, full_start)
            next_max = min(point[0] + half_span, full_end)
            if next_max - next_min < current_span:
                if next_min <= full_start:
                    next_max = min(full_start + current_span, full_end)
                else:
                    next_min = max(full_end - current_span, full_start)
            x_axis.setRange(next_min, next_max)
        if y_axis is not None:
            span = max(y_axis.max() - y_axis.min(), 1e-9)
            y_axis.setRange(display_point[1] - span / 2, display_point[1] + span / 2)

    def _keep_cursor_point_visible(self, point: tuple[float, float]) -> None:
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        if x_axis is not None:
            x_min = x_axis.min()
            x_max = x_axis.max()
            x_span = max(x_max - x_min, 1e-12)
            x_margin = x_span * 0.08
            next_min = x_min
            next_max = x_max
            if point[0] < x_min + x_margin:
                shift = (x_min + x_margin) - point[0]
                next_min = x_min - shift
                next_max = x_max - shift
            elif point[0] > x_max - x_margin:
                shift = point[0] - (x_max - x_margin)
                next_min = x_min + shift
                next_max = x_max + shift
            if next_min != x_min or next_max != x_max:
                x_axis.setRange(next_min, next_max)
        if y_axis is not None:
            y_min = y_axis.min()
            y_max = y_axis.max()
            y_span = max(y_max - y_min, 1e-12)
            y_margin = y_span * 0.08
            next_min = y_min
            next_max = y_max
            if point[1] < y_min + y_margin:
                shift = (y_min + y_margin) - point[1]
                next_min = y_min - shift
                next_max = y_max - shift
            elif point[1] > y_max - y_margin:
                shift = point[1] - (y_max - y_margin)
                next_min = y_min + shift
                next_max = y_max + shift
            if next_min != y_min or next_max != y_max:
                y_axis.setRange(next_min, next_max)

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

    def _clamp_value_to_waveform(self, point: tuple[float, float]) -> tuple[float, float]:
        if self.current_waveform is None:
            return point

        x_value, y_value = point
        if self.current_waveform.x_values:
            x_value = min(max(x_value, self.current_waveform.x_values[0]), self.current_waveform.x_values[-1])
        if self.current_waveform.y_values:
            y_min = min(self.current_waveform.y_values)
            y_max = max(self.current_waveform.y_values)
            y_value = min(max(y_value, y_min), y_max)
        return x_value, y_value

    def _clamp_cursor_point(self, point: tuple[float, float], channel: str | None) -> tuple[float, float]:
        if self.current_waveform is None:
            return point
        if channel is None:
            return self._clamp_value_to_waveform(point)

        x_value, y_value = point
        if self.current_waveform.x_values:
            x_value = min(max(x_value, self.current_waveform.x_values[0]), self.current_waveform.x_values[-1])
        return x_value, y_value

    def _point_visible_in_axes(self, point: tuple[float, float]) -> bool:
        x_axis = self._x_axis()
        y_axis = self._y_axis()
        if x_axis is not None and not (x_axis.min() <= point[0] <= x_axis.max()):
            return False
        if y_axis is not None and not (y_axis.min() <= point[1] <= y_axis.max()):
            return False
        return True

    def _display_cursor_point(self, point: tuple[float, float], channel: str | None) -> tuple[float, float]:
        if channel is None:
            return point
        return point[0], point[1] + self.waveform_offsets.get(channel, 0.0)

    def _raw_cursor_point(
        self,
        point: tuple[float, float] | None,
        channel: str | None,
    ) -> tuple[float, float] | None:
        if point is None:
            return None
        if channel is None:
            return point
        return point[0], point[1] - self.waveform_offsets.get(channel, 0.0)

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
            channel = self.cursor_channels.get(key)
            if point is None or not self._point_visible_in_axes(point):
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
            position = self.chart.mapToPosition(QPointF(point[0], point[1]), self.waveform_series)
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
            unit = self._channel_unit(channel) if channel is not None else self._current_waveform_unit()
            raw_point = self._raw_cursor_point(point, channel)
            y_text = raw_point[1] if raw_point is not None else point[1]
            self.cursor_text_items[key].setText(f"{color_name}\nt={point[0]:.3e}\nY={y_text:.3f} {unit}")
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
            self._hide_smart_preview()
            return
        if not self._point_visible_in_axes(point_a) or not self._point_visible_in_axes(point_b):
            self.lock_annotation_line.setVisible(False)
            self.lock_annotation_text_item.setVisible(False)
            return

        position_a = self.chart.mapToPosition(QPointF(point_a[0], point_a[1]), self.waveform_series)
        position_b = self.chart.mapToPosition(QPointF(point_b[0], point_b[1]), self.waveform_series)
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
            if not self._point_visible_in_axes(point):
                continue
            mapped = self.chart.mapToPosition(QPointF(point[0], point[1]), self.waveform_series)
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
        if not self.current_waveforms:
            return None
        plot_area = self.chart.plotArea()
        if plot_area.isEmpty() or not plot_area.contains(position):
            return None

        cursor_value = self.chart.mapToValue(position.toPoint())
        cursor_x = cursor_value.x()
        cursor_y = cursor_value.y()
        hit_candidates: list[tuple[float, str]] = []
        for waveform in self.current_waveforms:
            channel = waveform.channel
            if channel not in self.visible_channels:
                continue
            points = self.waveform_decimated_map.get(channel)
            if points is None:
                continue
            interpolated_y = _interpolate_waveform_y_at_x(points[0], points[1], cursor_x)
            if interpolated_y is None:
                continue
            display_y = interpolated_y + self.waveform_offsets.get(channel, 0.0)
            mapped = self.chart.mapToPosition(QPointF(cursor_x, display_y), self.waveform_series)
            distance_px = abs(position.y() - mapped.y())
            if distance_px <= 12:
                hit_candidates.append((distance_px, channel))

        if not hit_candidates:
            return None
        _, channel = min(hit_candidates, key=lambda item: item[0])
        return channel

    def _waveform_for_cursor_position(self, x_value: float, y_value: float, position) -> WaveformData | None:
        target_channel = self._waveform_drag_target_at(position)
        if target_channel is None:
            nearest_channel = self._nearest_channel_at_x(x_value, y_value)
            target_channel = nearest_channel
        if target_channel is None:
            return self._active_waveform()
        return next((waveform for waveform in self.current_waveforms if waveform.channel == target_channel), self._active_waveform())

    def _nearest_channel_at_x(self, x_value: float, y_value: float) -> str | None:
        best_match: tuple[float, str] | None = None
        for waveform in self.current_waveforms:
            channel = waveform.channel
            if channel not in self.visible_channels:
                continue
            points = self.waveform_source_map.get(channel)
            if points is None:
                continue
            interpolated_y = _interpolate_waveform_y_at_x(points[0], points[1], x_value)
            if interpolated_y is None:
                continue
            display_y = interpolated_y + self.waveform_offsets.get(channel, 0.0)
            distance = abs(display_y - y_value)
            if best_match is None or distance < best_match[0]:
                best_match = (distance, channel)
        return None if best_match is None else best_match[1]

    def _crosshair_label_text(self, x_value: float, y_value: float) -> str:
        lines = [f"t={x_value:.6e} s"]
        appended = False
        for waveform in self.current_waveforms:
            channel = waveform.channel
            if channel not in self.visible_channels:
                continue
            points = self.waveform_source_map.get(channel)
            if points is None:
                continue
            interpolated_y = _interpolate_waveform_y_at_x(points[0], points[1], x_value)
            if interpolated_y is None:
                continue
            unit = self._channel_unit(channel)
            lines.append(f"{display_channel_name(channel)}={interpolated_y:.6f} {unit}".strip())
            appended = True
        if not appended:
            lines.append(f"Y={y_value:.6f} {self._current_waveform_unit()}")
        return "\n".join(lines)

    def _channel_unit(self, channel: str) -> str:
        if self.channel_unit_resolver is not None:
            resolved = self.channel_unit_resolver(channel)
            if resolved:
                return resolved
        return "V"

    def _hover_cursor_shape(self, position) -> Qt.CursorShape:
        if self.dragging_waveform_channel is None:
            hover_channel = self._waveform_drag_target_at(position)
            if hover_channel is not None and hover_channel != self.hover_waveform_channel:
                self.hover_waveform_channel = hover_channel
                self._refresh_cursor_graphics()
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
                        self.cursor_hint_label.setText(f"当前命中 {display_channel_name(waveform_channel)}，可上下拖动分离显示。")
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

    def _update_smart_preview(self, position) -> None:
        if (
            self.current_waveform is None
            or self.pending_cursor_target is None
            or self.cursor_placement_mode != "smart"
            or self.waveform_series is None
        ):
            self._hide_smart_preview()
            return

        plot_area = self.chart.plotArea()
        if plot_area.isEmpty() or not plot_area.contains(position):
            self._hide_smart_preview()
            return

        mapped = self.chart.mapToValue(position.toPoint())
        target_waveform = self._waveform_for_cursor_position(mapped.x(), mapped.y(), position)
        preview_point = self._nearest_smart_point(mapped.x(), target_waveform)
        if preview_point is None:
            self._hide_smart_preview()
            return

        preview_channel = target_waveform.channel if target_waveform is not None else None
        display_preview_point = self._display_cursor_point(preview_point, preview_channel)
        preview_position = self.chart.mapToPosition(QPointF(display_preview_point[0], display_preview_point[1]), self.waveform_series)
        radius = 6
        self.smart_preview_item.setRect(
            preview_position.x() - radius,
            preview_position.y() - radius,
            radius * 2,
            radius * 2,
        )
        self.smart_preview_item.setVisible(True)
        preview_name = display_channel_name(preview_channel) if preview_channel is not None else "当前通道"
        self.smart_preview_label.setText(f"预判 {preview_name}\n t={preview_point[0]:.3e}")
        self.smart_preview_label.setPos(
            QPointF(
                min(preview_position.x() + 8, plot_area.right() - 72),
                max(preview_position.y() - 28, plot_area.top() + 4),
            )
        )
        self.smart_preview_label.setVisible(True)

    def _hide_smart_preview(self) -> None:
        self.smart_preview_item.setVisible(False)
        self.smart_preview_label.setVisible(False)

    def _cursor_pen(self, key: str, axis: str, active_mode: str | None) -> QPen:
        color = QColor(CURSOR_A_COLOR if key == "a" else CURSOR_B_COLOR)
        pen = QPen(color)
        pen.setWidth(4 if active_mode in {axis, "xy"} else 2)
        if active_mode in {axis, "xy"}:
            pen.setColor(color.lighter(115))
        return pen

    def _cursor_brush(self, key: str, active_mode: str | None):
        color = QColor(CURSOR_A_COLOR if key == "a" else CURSOR_B_COLOR)
        fill = QColor(color)
        fill.setAlpha(220 if active_mode == "xy" else 150)
        return fill

    def _cursor_mode_text(self, key: str, active_mode: str | None) -> str | None:
        if active_mode is None:
            return None
        return f"{key.upper()}-{active_mode.upper()}"

    def _default_cursor_hint(self) -> str:
        return "提示：在图上右键放置或更新游标，也可开启“游标联动拖动”；拖动竖线改时间，拖动横线改电压，拖动交点同时改两者；叠加通道可上下拖动分离显示。"

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
        is_hovered = channel == self.dragging_waveform_channel or channel == self.hover_waveform_channel
        is_active = channel == self.active_waveform_channel
        pen.setWidth(5 if is_hovered else (4 if is_active else 2))
        if is_hovered:
            pen.setColor(color.lighter(115))
        elif is_active:
            pen.setColor(color.lighter(108))
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

def _decimate_xy_envelope(x_values: list[float], y_values: list[float], max_points: int) -> tuple[list[float], list[float]]:
    point_count = min(len(x_values), len(y_values))
    if point_count <= max_points:
        return x_values[:point_count], y_values[:point_count]

    bucket_count = max(max_points // 2, 1)
    bucket_size = max((point_count + bucket_count - 1) // bucket_count, 1)
    reduced_x: list[float] = []
    reduced_y: list[float] = []

    for bucket_start in range(0, point_count, bucket_size):
        bucket_end = min(bucket_start + bucket_size, point_count)
        bucket_x = x_values[bucket_start:bucket_end]
        bucket_y = y_values[bucket_start:bucket_end]
        if not bucket_x:
            continue
        min_index = min(range(len(bucket_y)), key=bucket_y.__getitem__)
        max_index = max(range(len(bucket_y)), key=bucket_y.__getitem__)
        for index in sorted({min_index, max_index}):
            reduced_x.append(bucket_x[index])
            reduced_y.append(bucket_y[index])

    if not reduced_x or reduced_x[0] != x_values[0]:
        reduced_x.insert(0, x_values[0])
        reduced_y.insert(0, y_values[0])
    if reduced_x[-1] != x_values[point_count - 1]:
        reduced_x.append(x_values[point_count - 1])
        reduced_y.append(y_values[point_count - 1])
    return reduced_x, reduced_y


def _slice_xy_by_range(
    x_values: list[float],
    y_values: list[float],
    start_x: float,
    end_x: float,
) -> tuple[list[float], list[float]]:
    point_count = min(len(x_values), len(y_values))
    if point_count == 0:
        return [], []

    left = min(start_x, end_x)
    right = max(start_x, end_x)
    start_index = 0
    while start_index < point_count and x_values[start_index] < left:
        start_index += 1
    end_index = start_index
    while end_index < point_count and x_values[end_index] <= right:
        end_index += 1

    if start_index > 0:
        start_index -= 1
    if end_index < point_count:
        end_index += 1
    return x_values[start_index:end_index], y_values[start_index:end_index]


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
    return f"t={_format_time_value(point[0])}, V={point[1]:.6f}"


def _format_time_value(value: float) -> str:
    return format_engineering_value(value, "s")


def _format_optional_seconds(value: float | None) -> str:
    if value is None:
        return "无法估算"
    return _format_time_value(value)


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
