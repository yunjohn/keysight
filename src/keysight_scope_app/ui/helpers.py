from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QComboBox,
    QFrame,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QWidget,
)


def configure_high_dpi_policy() -> None:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def content_min_width(widget: QWidget, *, minimum_px: int, minimum_chars: int) -> int:
    return max(minimum_px, widget.fontMetrics().horizontalAdvance("0") * minimum_chars)


def create_scroll_area(
    parent: QWidget,
    content: QWidget,
    *,
    minimum_px: int,
    minimum_chars: int,
) -> QScrollArea:
    content.setMinimumWidth(
        content_min_width(parent, minimum_px=minimum_px, minimum_chars=minimum_chars)
    )
    scroll_area = QScrollArea(parent)
    scroll_area.setWidgetResizable(True)
    scroll_area.setFrameShape(QFrame.NoFrame)
    scroll_area.setWidget(content)
    return scroll_area


def set_uniform_control_height(
    container: QWidget,
    *,
    height: int | None = None,
) -> None:
    if height is None:
        combo_heights = [
            combo.sizeHint().height()
            for combo in container.findChildren(QComboBox)
        ]
        height = max(combo_heights) if combo_heights else 24

    for button in container.findChildren(QPushButton):
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setMinimumHeight(height)


def set_equal_button_widths(
    *buttons: QPushButton,
    minimum_width: int | None = None,
) -> None:
    visible_buttons = [button for button in buttons if button is not None]
    if not visible_buttons:
        return
    width = max(button.sizeHint().width() for button in visible_buttons)
    if minimum_width is not None:
        width = max(width, minimum_width)
    for button in visible_buttons:
        button.setMinimumWidth(width)
        button.setMaximumWidth(width)


def apply_responsive_window_geometry(
    window: QWidget,
    *,
    minimum_width: int,
    minimum_height: int,
    preferred_width: int | None = None,
    preferred_height: int | None = None,
    extra_width: int = 0,
    extra_height: int = 0,
    margin_width: int = 40,
    margin_height: int = 60,
) -> None:
    window.adjustSize()
    screen = window.screen() or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        max_width = max(available.width() - margin_width, minimum_width)
        max_height = max(available.height() - margin_height, minimum_height)
    else:
        max_width = max(preferred_width or 1600, minimum_width)
        max_height = max(preferred_height or 1000, minimum_height)

    size_hint = window.sizeHint().expandedTo(window.minimumSizeHint())
    target_width = preferred_width if preferred_width is not None else size_hint.width() + extra_width
    target_height = preferred_height if preferred_height is not None else size_hint.height() + extra_height
    target_width = min(max(target_width, minimum_width), max_width)
    target_height = min(max(target_height, minimum_height), max_height)

    window.setMinimumSize(min(minimum_width, target_width), min(minimum_height, target_height))
    window.resize(target_width, target_height)


def display_channel_name(channel: str) -> str:
    if channel.startswith("CHANnel"):
        return channel.replace("CHANnel", "CH", 1)
    return channel


def normalize_channel_name(channel: str) -> str:
    normalized = channel.strip()
    if normalized.upper().startswith("CH") and normalized[2:].isdigit():
        return f"CHANnel{normalized[2:]}"
    return normalized


def format_peak_current(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.value:.6f} A"


def format_peak_time(peak) -> str:
    if peak is None:
        return "-"
    return f"{peak.time_s:.6e} s"


def format_range_ms(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.3f} ~ {max(values):.3f} ms"


def format_range_amp(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} A"


def format_range_hz(values: list[float]) -> str:
    if not values:
        return "-"
    return f"{min(values):.6f} ~ {max(values):.6f} Hz"
