from PySide6.QtWidgets import QAbstractButton, QApplication

from keysight_scope_app.analysis.waveform import WaveformData, WaveformPreamble
from keysight_scope_app.ui.dialogs.waveform import (
    WAVEFORM_SIDEBAR_WIDTH,
    WAVEFORM_SIDEBAR_TAB_HEIGHT,
    WaveformDetailDialog,
)
from keysight_scope_app.ui.panels.waveform import _decimate_xy_envelope, _slice_xy_by_range
from keysight_scope_app.ui.waveform_navigator import WaveformNavigator
from keysight_scope_app.ui.waveform_theme import InteractionTool


def test_million_point_envelope_preserves_narrow_spike_and_endpoints() -> None:
    count = 1_000_000
    x_values = [float(index) for index in range(count)]
    y_values = [0.0] * count
    y_values[543_210] = 99.0
    reduced_x, reduced_y = _decimate_xy_envelope(x_values, y_values, max_points=2400)
    assert reduced_x[0] == 0
    assert reduced_x[-1] == count - 1
    assert max(reduced_y) == 99.0
    assert len(reduced_x) <= 2402


def test_visible_slice_keeps_neighbor_samples_for_interpolation() -> None:
    x_values = [0.0, 1.0, 2.0, 3.0]
    y_values = [0.0, 1.0, 4.0, 9.0]
    visible_x, visible_y = _slice_xy_by_range(x_values, y_values, 1.2, 1.8)
    assert visible_x == [1.0, 2.0]
    assert visible_y == [1.0, 4.0]


def test_waveform_workspace_controls_and_status_smoke() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    waveform = WaveformData(
        "CHANnel1",
        "NORMal",
        WaveformPreamble(0, 0, 5, 1, 0.001, 0, 0, 1, 0, 0),
        [0, 0.001, 0.002, 0.003, 0.004],
        [0, 1, 0, 1, 0],
    )
    dialog = WaveformDetailDialog()
    try:
        dialog.set_waveforms([waveform])
        assert "原始点：5" in dialog.workspace_status_label.text()
        assert dialog.event_list.count() >= 2
        assert "离线/单次" in dialog.workspace_status_label.text()
        dialog._set_interaction_tool(InteractionTool.PAN)
        assert dialog.analysis_panel.interaction_tool is InteractionTool.PAN
        assert dialog.tool_capsule.text() == "平移"
        assert dialog.sidebar_toolbox.count() == 7
        dialog.sidebar_toggle_button.setChecked(True)
        assert not dialog.advanced_controls_widget.isVisible()
    finally:
        dialog.close()


def test_waveform_sidebar_has_fixed_width_and_unclipped_measurement_buttons() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    dialog = WaveformDetailDialog()
    try:
        dialog.show()
        app.processEvents()
        assert dialog.advanced_controls_widget.minimumWidth() == WAVEFORM_SIDEBAR_WIDTH
        assert dialog.advanced_controls_widget.maximumWidth() == WAVEFORM_SIDEBAR_WIDTH
        sidebar_buttons = (
            dialog.freeze_measurements_button,
            dialog.measurement_settings_button,
            dialog.export_phase_diagnostics_button,
            dialog.add_annotation_button,
            dialog.load_reference_button,
            dialog.add_bookmark_button,
        )
        for button in sidebar_buttons:
            assert button.maximumWidth() > WAVEFORM_SIDEBAR_WIDTH
            assert button.sizePolicy().horizontalPolicy().name == "Expanding"
        tab_buttons = dialog.sidebar_toolbox.findChildren(
            QAbstractButton,
            "qt_toolbox_toolboxbutton",
        )
        assert len(tab_buttons) == dialog.sidebar_toolbox.count()
        for button in tab_buttons:
            assert button.minimumHeight() == WAVEFORM_SIDEBAR_TAB_HEIGHT
            assert button.height() >= button.fontMetrics().height() + 8
        dialog.measurement_text_label.setText("测量内容<br>第二行")
        dialog.cursor_text_label.setText("游标内容<br>第二行")
        dialog.measurement_overlay.show()
        dialog.cursor_overlay.show()
        dialog._set_overlay_collapsed("measurement", False)
        dialog._set_overlay_collapsed("cursor", False)
        expanded_measurement_height = dialog.measurement_overlay.height()
        expanded_cursor_height = dialog.cursor_overlay.height()
        dialog._set_overlay_collapsed("measurement", True)
        dialog._set_overlay_collapsed("cursor", True)
        assert dialog.measurement_text_label.isHidden()
        assert dialog.cursor_text_label.isHidden()
        assert dialog.measurement_overlay_collapse_button.text() == "+"
        assert dialog.cursor_overlay_collapse_button.text() == "+"
        assert dialog.measurement_overlay.height() < expanded_measurement_height
        assert dialog.cursor_overlay.height() < expanded_cursor_height
    finally:
        dialog.close()


def test_waveform_navigator_clamps_view_to_full_range() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    navigator = WaveformNavigator()
    navigator.set_ranges((0.0, 10.0), (-2.0, 4.0))
    assert navigator.view_range == (0.0, 6.0)
    navigator.set_ranges((0.0, 10.0), (8.0, 14.0))
    assert navigator.view_range == (4.0, 10.0)


def test_waveforms_auto_stack_in_channel_order_without_overlap() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    waveforms = [
        WaveformData(
            channel,
            "NORMal",
            WaveformPreamble(0, 0, 3, 1, 0.001, 0, 0, 1, 0, 0),
            [0.0, 0.001, 0.002],
            values,
        )
        for channel, values in (
            ("CHANnel4", [-4.0, 0.0, 4.0]),
            ("CHANnel2", [-1.0, 0.0, 1.0]),
            ("CHANnel1", [9.0, 10.0, 11.0]),
            ("CHANnel3", [0.0, 0.5, 1.0]),
        )
    ]
    dialog = WaveformDetailDialog()
    try:
        dialog.set_waveforms(waveforms)
        displayed_bounds = {}
        for waveform in waveforms:
            offset = dialog.analysis_panel.waveform_offsets[waveform.channel]
            displayed_bounds[waveform.channel] = (
                min(waveform.y_values) + offset,
                max(waveform.y_values) + offset,
            )
        for upper_channel, lower_channel in (
            ("CHANnel1", "CHANnel2"),
            ("CHANnel2", "CHANnel3"),
            ("CHANnel3", "CHANnel4"),
        ):
            assert displayed_bounds[upper_channel][0] > displayed_bounds[lower_channel][1]
    finally:
        dialog.close()
