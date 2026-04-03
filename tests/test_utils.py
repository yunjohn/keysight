from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pyvisa.errors import VisaIOError

from keysight_scope_app.device.instrument import (
    EdgeTriggerSettings,
    KeysightOscilloscope,
    StartupBrakeTestConfig,
    WaveformData,
    WaveformPreamble,
    analyze_startup_brake_test,
    compare_waveform_edges,
)
from keysight_scope_app.ui.dialogs import waveform as waveform_dialog_module
from keysight_scope_app.ui.dialogs.waveform import WaveformDetailDialog
from keysight_scope_app.ui import main_window as main_window_module
from keysight_scope_app.ui.main_window import ScopeMainWindow
from keysight_scope_app.ui.panels.waveform import _should_apply_scope_vertical_layouts
from keysight_scope_app.utils import format_engineering_value, strip_ieee4882_block


def test_strip_ieee4882_block_extracts_payload() -> None:
    payload = b"PNGDATA"
    raw = b"#17" + payload + b"\n"
    assert strip_ieee4882_block(raw) == payload


def test_format_engineering_value_scales_voltage() -> None:
    assert format_engineering_value(0.0025, "V") == "2.5 mV"


def test_format_engineering_value_marks_invalid_sentinel() -> None:
    assert format_engineering_value(9.9e37, "V") == "无效"


def test_waveform_export_csv_writes_header_and_values(tmp_path: Path) -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 2, 1, 1e-6, 0.0, 0, 0.01, 0.0, 128),
        x_values=[0.0, 1e-6],
        y_values=[0.1, 0.2],
    )
    target = waveform.export_csv(tmp_path / "waveform.csv")

    content = target.read_text(encoding="utf-8")
    assert "time_s,voltage_v" in content
    assert "0.000000000000e+00,1.000000000000e-01" in content


def test_waveform_from_csv_and_analyze(tmp_path: Path) -> None:
    source = tmp_path / "waveform.csv"
    source.write_text(
        "time_s,voltage_v\n"
        "0.0,0.0\n"
        "0.25,1.0\n"
        "0.5,0.0\n"
        "0.75,-1.0\n"
        "1.0,0.0\n"
        "1.25,1.0\n"
        "1.5,0.0\n"
        "1.75,-1.0\n"
        "2.0,0.0\n",
        encoding="utf-8",
    )

    waveform = WaveformData.from_csv(source)
    stats = waveform.analyze()

    assert waveform.channel == "CSV"
    assert stats.point_count == 9
    assert stats.voltage_pp == 2.0
    assert stats.estimated_frequency_hz == 1.0


def test_waveform_bundle_export_and_load_round_trip(tmp_path: Path) -> None:
    waveform_a = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 3, 1, 1.0, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 1.0, 2.0],
        y_values=[0.0, 1.0, 0.0],
    )
    waveform_b = WaveformData(
        channel="CHANnel2",
        points_mode="RAW",
        preamble=WaveformPreamble(0, 0, 3, 1, 1.0, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 1.0, 2.0],
        y_values=[1.0, 0.0, 1.0],
    )
    target = tmp_path / "bundle.csv"

    WaveformData.export_csv_bundle([waveform_a, waveform_b], target)
    loaded = WaveformData.load_csv_bundle(target)

    assert len(loaded) == 2
    assert loaded[0].channel == "CHANnel1"
    assert loaded[0].points_mode == "NORMal"
    assert loaded[0].y_values == [0.0, 1.0, 0.0]
    assert loaded[1].channel == "CHANnel2"
    assert loaded[1].points_mode == "RAW"
    assert loaded[1].y_values == [1.0, 0.0, 1.0]


def test_waveform_analyze_and_snap_to_edge() -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )

    stats = waveform.analyze()
    rising = waveform.snap_to_edge(0.9, "rising")
    falling = waveform.snap_to_edge(0.9, "falling")

    assert stats.estimated_frequency_hz == 1.0
    assert stats.pulse_width_s == 0.5
    assert stats.duty_cycle == 0.5
    assert abs(stats.rise_time_s - 0.2) < 1e-9
    assert abs(stats.fall_time_s - 0.2) < 1e-9
    assert rising == (1.125, 0.5)
    assert falling == (0.625, 0.5)


def test_waveform_find_nearest_pulse() -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 11, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0],
    )

    pulse = waveform.find_nearest_pulse(1.4)

    assert pulse is not None
    assert pulse.rising_edge == (1.125, 0.5)
    assert pulse.falling_edge == (1.625, 0.5)


def test_waveform_find_nearest_period() -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 13, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )

    period = waveform.find_nearest_period(1.4, edge_type="rising")

    assert period is not None
    assert period.edge_type == "rising"
    assert period.start_edge == (1.125, 0.5)
    assert period.end_edge == (2.125, 0.5)


def test_waveform_recommend_lock_window() -> None:
    periodic_waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 13, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )
    pulse_waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 7, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    )

    periodic_lock = periodic_waveform.recommend_lock_window(1.4)
    pulse_lock = pulse_waveform.recommend_lock_window(0.5)

    assert periodic_lock is not None
    assert periodic_lock.mode == "period"
    assert periodic_lock.start_edge == (1.125, 0.5)
    assert periodic_lock.end_edge == (2.125, 0.5)

    assert pulse_lock is not None
    assert pulse_lock.mode == "pulse"
    assert pulse_lock.start_edge == (0.125, 0.5)
    assert pulse_lock.end_edge == (0.625, 0.5)


def test_waveform_analyze_window() -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )

    stats = waveform.analyze_window(0.0, 1.0)

    assert stats is not None
    assert stats.point_count == 5
    assert stats.duration_s == 1.0
    assert stats.voltage_pp == 1.0
    assert stats.pulse_width_s == 0.5


def test_fetch_measurements_supports_derived_metrics_with_single_waveform_fetch() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")
            self.waveform_fetch_count = 0

        def query(self, command: str) -> str:
            if command == ":MEASure:FREQuency? CHANnel1":
                return "1000.0"
            raise AssertionError(f"unexpected query: {command}")

        def fetch_waveform(
            self,
            channel: str,
            *,
            points_mode: str = "NORMal",
            points: int = 1000,
        ) -> WaveformData:
            self.waveform_fetch_count += 1
            assert channel == "CHANnel1"
            assert points_mode == "NORMal"
            return WaveformData(
                channel="CHANnel1",
                points_mode="NORMal",
                preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
                x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
                y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
            )

    scope = FakeScope()
    results = scope.fetch_measurements("CHANnel1", ["频率", "脉冲计数", "占空比", "正脉宽", "负脉宽", "下降时间", "高电平估计"])
    result_map = {result.label: result.raw_value for result in results}

    assert scope.waveform_fetch_count == 1
    assert result_map["频率"] == 1000.0
    assert result_map["脉冲计数"] == 2.0
    assert result_map["占空比"] == 50.0
    assert result_map["正脉宽"] == 0.5
    assert result_map["负脉宽"] == 0.5
    assert abs(result_map["下降时间"] - 0.2) < 1e-9
    assert result_map["高电平估计"] == 1.0


def test_fetch_waveform_applies_requested_points_to_instrument() -> None:
    class FakeInstrument:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def write(self, command: str) -> None:
            self.commands.append(command)

        def query_ascii_values(self, command: str) -> list[float]:
            assert command == ":WAVeform:PREamble?"
            return [0, 0, 5, 1, 1e-6, 0.0, 0, 0.01, 0.0, 128]

        def query_binary_values(
            self,
            command: str,
            *,
            datatype: str,
            container,
            header_fmt: str,
            expect_termination: bool,
        ) -> list[int]:
            assert command == ":WAVeform:DATA?"
            assert datatype == "B"
            assert container is list
            assert header_fmt == "ieee"
            assert expect_termination is False
            return [128, 129, 130, 131, 132]

    scope = KeysightOscilloscope("USB::TEST")
    fake_instrument = FakeInstrument()
    scope._instrument = fake_instrument

    waveform = scope.fetch_waveform("CHANnel2", points_mode="RAW", points=5)

    assert fake_instrument.commands == [
        ":WAVeform:SOURce CHANnel2",
        ":WAVeform:FORMat BYTE",
        ":WAVeform:UNSigned ON",
        ":WAVeform:POINts:MODE RAW",
        ":WAVeform:POINts 5",
    ]
    assert waveform.channel == "CHANnel2"
    assert waveform.points_mode == "RAW"
    assert waveform.preamble.points == 5
    assert len(waveform.x_values) == 5
    assert len(waveform.y_values) == 5
    assert waveform.x_values == [0.0, 1e-6, 2e-6, 3e-6, 4e-6]
    assert waveform.y_values == [0.0, 0.01, 0.02, 0.03, 0.04]


def test_get_displayed_channels_reads_scope_display_state() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")

        def query(self, command: str) -> str:
            responses = {
                ":CHANnel1:DISPlay?": "1",
                ":CHANnel2:DISPlay?": "0",
                ":CHANnel3:DISPlay?": "ON",
                ":CHANnel4:DISPlay?": "OFF",
            }
            return responses[command]

    scope = FakeScope()

    assert scope.get_displayed_channels() == ["CHANnel1", "CHANnel3"]


def test_get_channel_unit_reads_scope_unit_setting() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")

        def query(self, command: str) -> str:
            responses = {
                ":CHANnel1:UNITs?": "VOLT",
                ":CHANnel3:UNITs?": "AMPere",
            }
            return responses[command]

    scope = FakeScope()

    assert scope.get_channel_unit("CHANnel1") == "V"
    assert scope.get_channel_unit("CHANnel3") == "A"


def test_scope_vertical_layouts_disabled_for_mixed_units() -> None:
    unit_map = {
        "CHANnel1": "V",
        "CHANnel3": "A",
    }

    assert not _should_apply_scope_vertical_layouts(
        ["CHANnel1", "CHANnel3"],
        lambda channel: unit_map[channel],
    )
    assert _should_apply_scope_vertical_layouts(
        ["CHANnel1"],
        lambda channel: unit_map.get(channel, "V"),
    )


def test_fetch_measurements_uses_current_units_for_channel_3() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")

        def query(self, command: str) -> str:
            responses = {
                ":CHANnel3:UNITs?": "AMPere",
                ":MEASure:VPP? CHANnel3": "2.5",
            }
            if command in responses:
                return responses[command]
            raise AssertionError(f"unexpected query: {command}")

    scope = FakeScope()

    results = scope.fetch_measurements("CHANnel3", ["峰峰值"])

    assert len(results) == 1
    assert results[0].raw_value == 2.5
    assert results[0].unit == "A"
    assert results[0].display_value == "2.5 A"


def test_fetch_measurements_timeout_on_single_item_does_not_abort_all_results() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")

        def query(self, command: str) -> str:
            if command == ":CHANnel1:UNITs?":
                return "VOLT"
            if command == ":MEASure:FREQuency? CHANnel1":
                raise VisaIOError(-1073807339)
            if command == ":MEASure:VPP? CHANnel1":
                return "2.5"
            raise AssertionError(f"unexpected query: {command}")

    scope = FakeScope()

    results = scope.fetch_measurements("CHANnel1", ["频率", "峰峰值"])

    assert len(results) == 2
    assert results[0].label == "频率"
    assert results[0].display_value == "超时"
    assert results[1].label == "峰峰值"
    assert results[1].display_value == "2.5 V"


def test_main_window_channel_unit_auto_label_updates_from_detected_units() -> None:
    app = QApplication.instance() or QApplication([])
    window = ScopeMainWindow()
    try:
        window._update_channel_units({"CHANnel1": "A"}, log_message=False)
        assert window.channel_unit_combos["CHANnel1"].itemText(0) == "自动(A)"
        assert window._channel_unit("CHANnel1") == "A"
    finally:
        window.close()


def test_main_window_channel_unit_manual_override_takes_precedence() -> None:
    app = QApplication.instance() or QApplication([])
    window = ScopeMainWindow()
    try:
        window._update_channel_units({"CHANnel2": "V"}, log_message=False)
        window._set_channel_unit_override("CHANnel2", "A")
        assert window._channel_unit("CHANnel2") == "A"
        assert window._channel_unit_status_text("CHANnel2") == "A（手动）"
        window._set_channel_unit_override("CHANnel2", None)
        assert window._channel_unit("CHANnel2") == "V"
        assert window._channel_unit_status_text("CHANnel2") == "V（自动）"
    finally:
        window.close()


def test_get_edge_trigger_settings_reads_scope_values() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")

        def query(self, command: str) -> str:
            responses = {
                ":TRIGger:EDGE:SOURce?": "CHAN1",
                ":TRIGger:EDGE:SLOPe?": "NEG",
                ":TRIGger:EDGE:LEVel?": "1.25",
                ":TRIGger:SWEep?": "AUTO",
            }
            return responses[command]

    scope = FakeScope()

    settings = scope.get_edge_trigger_settings()

    assert settings == EdgeTriggerSettings(
        source="CHANnel1",
        slope="NEGative",
        level=1.25,
        sweep="AUTO",
    )


def test_apply_edge_trigger_settings_writes_expected_commands() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self) -> None:
            super().__init__("USB::TEST")
            self.commands: list[str] = []

        def write(self, command: str) -> None:
            self.commands.append(command)

    scope = FakeScope()

    scope.apply_edge_trigger_settings(
        EdgeTriggerSettings(
            source="CHANnel2",
            slope="POSitive",
            level=0.75,
            sweep="NORMal",
        )
    )

    assert scope.commands == [
        ":TRIGger:MODE EDGE",
        ":TRIGger:EDGE:SOURce CHANnel2",
        ":TRIGger:EDGE:SLOPe POSitive",
        ":TRIGger:EDGE:LEVel 0.75",
        ":TRIGger:SWEep NORMal",
    ]


def test_get_trigger_event_status_reads_ter_query() -> None:
    class FakeScope(KeysightOscilloscope):
        def __init__(self, response: str) -> None:
            super().__init__("USB::TEST")
            self.response = response

        def query(self, command: str) -> str:
            assert command == ":TER?"
            return self.response

    assert FakeScope("1").get_trigger_event_status() is True
    assert FakeScope("0").get_trigger_event_status() is False


def test_waveform_detail_dialog_persists_measurement_settings(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    original_path = waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH
    original_dir = waveform_dialog_module.WAVEFORM_CONFIG_DIR
    waveform_dialog_module.WAVEFORM_CONFIG_DIR = tmp_path
    waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH = tmp_path / "waveform_measurements.json"
    try:
        first = WaveformDetailDialog()
        first.measurement_config = {
            "CHANnel1": {"频率", "峰峰值"},
            "CHANnel3": {"均方根", "最大值"},
        }
        first._save_measurement_config()
        first.close()

        second = WaveformDetailDialog()
        assert second.measurement_config == {
            "CHANnel1": {"频率", "峰峰值"},
            "CHANnel3": {"均方根", "最大值"},
        }
        second.close()
    finally:
        waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH = original_path
        waveform_dialog_module.WAVEFORM_CONFIG_DIR = original_dir


def test_waveform_detail_dialog_ignores_corrupt_measurement_settings(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    original_path = waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH
    original_dir = waveform_dialog_module.WAVEFORM_CONFIG_DIR
    waveform_dialog_module.WAVEFORM_CONFIG_DIR = tmp_path
    waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH = tmp_path / "waveform_measurements.json"
    waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH.write_text("{bad json", encoding="utf-8")
    try:
        dialog = WaveformDetailDialog()
        assert dialog.measurement_config == {}
        dialog.close()
    finally:
        waveform_dialog_module.WAVEFORM_MEASUREMENT_SETTINGS_PATH = original_path
        waveform_dialog_module.WAVEFORM_CONFIG_DIR = original_dir


def test_waveform_dialog_formats_missing_measurement_as_dashdash() -> None:
    waveform = WaveformData(
        channel="CHANnel1",
        points_mode="RAW",
        preamble=WaveformPreamble(0, 0, 4, 1, 1.0, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 1.0, 2.0, 3.0],
        y_values=[0.0, 1.0, 1.0, 0.0],
    )
    dialog = WaveformDetailDialog()
    try:
        dialog.measurement_config = {"CHANnel1": {"频率", "峰峰值"}}
        html = dialog._build_measurement_section_html(waveform)
        assert "频率" in html
        assert "--" in html
    finally:
        dialog.close()


def test_waveform_dialog_formats_pulse_count_as_integer() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = WaveformDetailDialog()
    try:
        waveform = WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
            x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
            y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
        )
        dialog.set_waveform(waveform, waveform.analyze())
        dialog.measurement_config = {"CHANnel1": {"脉冲计数"}}
        html = dialog._build_measurement_section_html(waveform, "full")
        assert "脉冲计数" in html
        assert "2 个" in html
    finally:
        dialog.close()


def test_main_window_persists_waveform_mode_and_points(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    original_ui_state_path = main_window_module.UI_STATE_PATH
    main_window_module.UI_STATE_PATH = tmp_path / "ui_state.json"
    try:
        first = ScopeMainWindow()
        first.waveform_mode_combo.setCurrentText("RAW")
        first.waveform_points_input.setValue(54321)
        first._save_ui_state()
        first.close()

        second = ScopeMainWindow()
        assert second.waveform_mode_combo.currentText() == "RAW"
        assert int(second.waveform_points_input.value()) == 54321
        second.close()
    finally:
        main_window_module.UI_STATE_PATH = original_ui_state_path


def test_main_window_persists_trigger_settings(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    original_ui_state_path = main_window_module.UI_STATE_PATH
    main_window_module.UI_STATE_PATH = tmp_path / "ui_state.json"
    try:
        first = ScopeMainWindow()
        first._apply_trigger_settings_to_controls(
            EdgeTriggerSettings(
                source="CHANnel3",
                slope="NEGative",
                level=1.5,
                sweep="NORMal",
            )
        )
        first.close()

        second = ScopeMainWindow()
        settings = second._current_trigger_settings()
        assert settings == EdgeTriggerSettings(
            source="CHANnel3",
            slope="NEGative",
            level=1.5,
            sweep="NORMal",
        )
        second.close()
    finally:
        main_window_module.UI_STATE_PATH = original_ui_state_path


def test_compare_waveform_edges_returns_delay_and_phase() -> None:
    primary = WaveformData(
        channel="CHANnel1",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )
    secondary = WaveformData(
        channel="CHANnel2",
        points_mode="NORMal",
        preamble=WaveformPreamble(0, 0, 9, 1, 0.25, 0.0, 0, 1.0, 0.0, 0),
        x_values=[0.1, 0.35, 0.6, 0.85, 1.1, 1.35, 1.6, 1.85, 2.1],
        y_values=[0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
    )

    comparison = compare_waveform_edges(primary, secondary, 1.2, "rising", frequency_hz=1.0)

    assert comparison is not None
    assert abs(comparison.delta_t_s - 0.1) < 1e-9
    assert abs(comparison.phase_deg - 36.0) < 1e-9


def test_analyze_startup_brake_test_current_zero_mode() -> None:
    waveforms = _build_startup_brake_waveforms()
    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            encoder_a_channel="CHANnel4",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            zero_current_threshold_a=0.05,
            brake_mode="current_zero",
        ),
    )

    assert abs(result.startup_start_point[0] - 0.0191) < 1e-9
    assert abs(result.speed_reached_point[0] - 0.0395) < 1e-9
    assert abs(result.startup_delay_s - 0.0204) < 1e-9
    assert result.startup_peak_current is not None
    assert result.startup_peak_current.value == 2.0
    assert result.startup_peak_current.time_s == 0.02
    assert abs(result.brake_start_point[0] - 0.07902) < 1e-9
    assert abs(result.current_zero_window.start_time_s - 0.086) < 1e-9
    assert abs(result.current_zero_window.confirmed_time_s - 0.089) < 1e-9
    assert abs(result.brake_end_point[0] - 0.086) < 1e-9
    assert abs(result.brake_delay_s - 0.00698) < 1e-9


def test_analyze_startup_brake_test_current_zero_mode_does_not_require_encoder_channel() -> None:
    waveforms = [waveform for waveform in _build_startup_brake_waveforms() if waveform.channel != "CHANnel4"]
    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            encoder_a_channel="CHANnel4",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            zero_current_threshold_a=0.05,
            brake_mode="current_zero",
        ),
    )

    assert abs(result.current_zero_window.confirmed_time_s - 0.089) < 1e-9
    assert abs(result.brake_end_point[0] - 0.086) < 1e-9


def test_analyze_startup_brake_test_encoder_backtrack_mode() -> None:
    waveforms = _build_startup_brake_waveforms()
    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            encoder_a_channel="CHANnel4",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            zero_current_threshold_a=0.05,
            brake_mode="encoder_backtrack",
            brake_backtrack_pulses=1,
        ),
    )

    assert result.current_zero_window is None
    assert abs(result.brake_end_point[0] - 0.0849) < 1e-9
    assert abs(result.brake_delay_s - 0.00588) < 1e-9


def test_analyze_startup_brake_test_encoder_backtrack_ignores_small_noise_pulses() -> None:
    waveforms = _build_startup_brake_waveforms()
    encoder_waveform = next(waveform for waveform in waveforms if waveform.channel == "CHANnel4")
    encoder_waveform.x_values[:] = [0.0846, 0.0848, 0.085, 0.0852, 0.0854, 0.0856, 0.0858, 0.086, 0.0862, 0.0864]
    encoder_waveform.y_values[:] = [0.0, 0.0, 0.4, 0.0, 5.0, 0.0, 0.3, 0.0, 5.0, 0.0]

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            encoder_a_channel="CHANnel4",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            zero_current_threshold_a=0.05,
            brake_mode="encoder_backtrack",
            brake_backtrack_pulses=2,
            brake_backtrack_min_step=1.0,
            brake_backtrack_min_interval_s=0.0005,
        ),
    )

    assert result.current_zero_window is None
    assert result.brake_end_point is not None
    assert abs(result.brake_end_point[0] - 0.0853) < 1e-9


def test_analyze_startup_brake_test_startup_only_mode() -> None:
    waveforms = _build_startup_brake_waveforms()
    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            test_scope_mode="startup_only",
        ),
    )

    assert result.startup_delay_s is not None
    assert result.speed_match is not None
    assert result.brake_delay_s is None
    assert result.brake_start_point is None


def test_analyze_startup_brake_test_brake_only_mode() -> None:
    waveforms = _build_startup_brake_waveforms()
    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.startup_delay_s is None
    assert result.speed_match is None
    assert result.brake_delay_s is not None
    assert result.current_zero_window is not None


def test_analyze_startup_brake_test_ignores_small_control_voltage_fluctuation() -> None:
    waveforms = _build_startup_brake_waveforms()
    control_waveform = next(waveform for waveform in waveforms if waveform.channel == "CHANnel1")
    control_waveform.y_values[5] = 0.4
    control_waveform.y_values[6] = 0.6
    control_waveform.y_values[7] = 0.3

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            startup_min_voltage_step=1.0,
            startup_hold_s=0.001,
            test_scope_mode="startup_only",
        ),
    )

    assert result.startup_start_point is not None
    assert abs(result.startup_start_point[0] - 0.0191) < 1e-9


def test_analyze_startup_brake_test_brake_start_uses_control_falling_edge_before_speed_zero() -> None:
    waveforms = _build_startup_brake_waveforms()

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert abs(result.brake_start_point[0] - 0.07902) < 1e-9


def test_analyze_startup_brake_test_brake_start_ignores_early_false_control_drop() -> None:
    waveforms = _build_false_brake_then_real_brake_waveforms()

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="frequency_hz",
            speed_target_value=100.0,
            speed_consecutive_periods=2,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert abs(result.brake_start_point[0] - 0.07902) < 1e-9


def test_analyze_startup_brake_test_brake_start_falls_back_to_speed_zero_reference() -> None:
    waveforms = _build_startup_brake_waveforms()

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_tolerance_ratio=0.01,
            speed_consecutive_periods=2,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert abs(result.brake_start_point[0] - 0.07902) < 1e-9


def test_analyze_startup_brake_test_brake_start_ignores_high_level_ripple_before_real_drop() -> None:
    waveforms = _build_rippled_brake_control_waveforms()

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_tolerance_ratio=0.01,
            speed_consecutive_periods=3,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert abs(result.brake_start_point[0] - 0.07802360615521856) < 1e-9


def test_analyze_startup_brake_test_brake_start_accepts_low_region_with_short_rebound() -> None:
    waveforms = _build_brake_control_with_short_low_rebound_waveforms()

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_tolerance_ratio=0.01,
            speed_consecutive_periods=3,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert abs(result.brake_start_point[0] - 0.07802) < 1e-6


def test_analyze_startup_brake_test_current_zero_mode_accepts_probe_jitter_near_zero() -> None:
    source_path = Path("captures/waveforms/test.csv")
    if not source_path.exists():
        pytest.skip("captures/waveforms/test.csv 不存在。")
    waveforms = WaveformData.load_csv_bundle(source_path)

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel4",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_consecutive_periods=3,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
        ),
    )

    assert result.brake_start_point is not None
    assert result.current_zero_window is not None
    assert result.brake_end_point is not None
    assert result.brake_delay_s is not None


def test_analyze_startup_brake_test_current_zero_mode_accepts_small_zero_bias() -> None:
    source_path = Path("captures/waveforms/test1.csv")
    if not source_path.exists():
        pytest.skip("captures/waveforms/test1.csv 不存在。")
    waveforms = WaveformData.load_csv_bundle(source_path)

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel4",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_consecutive_periods=3,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
            control_threshold_ratio=0.1,
        ),
    )

    assert result.brake_start_point is not None
    assert result.current_zero_window is not None
    assert result.brake_end_point is not None
    assert result.brake_delay_s is not None


def test_analyze_startup_brake_test_current_zero_mode_delays_false_zero_segment() -> None:
    source_path = Path("captures/waveforms/bundle_20260326_172459.csv")
    if not source_path.exists():
        pytest.skip("captures/waveforms/bundle_20260326_172459.csv 不存在。")
    waveforms = WaveformData.load_csv_bundle(source_path)

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_tolerance_ratio=0.01,
            speed_consecutive_periods=3,
            test_scope_mode="full",
            brake_mode="current_zero",
        ),
    )

    assert result.current_zero_window is not None
    assert abs(result.current_zero_window.start_time_s - (-6.23258006)) < 1e-9


def test_analyze_startup_brake_test_current_zero_mode_skips_rebounding_zero_candidate() -> None:
    control_x = [index * 0.001 for index in range(121)]
    control_y = [10.0 if time_value < 0.04 else 0.0 for time_value in control_x]

    speed_x = control_x
    speed_y = [5.0 if time_value < 0.085 else 0.0 for time_value in speed_x]

    current_x = control_x
    current_y: list[float] = []
    for time_value in current_x:
        if time_value < 0.05:
            current_y.append(4.0)
        elif time_value < 0.053:
            current_y.append(0.0)
        elif time_value < 0.058:
            current_y.append(2.5)
        elif time_value < 0.09:
            current_y.append(0.0)
        else:
            current_y.append(0.0)

    waveforms = [
        WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(control_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=control_x,
            y_values=control_y,
        ),
        WaveformData(
            channel="CHANnel2",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(speed_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=speed_x,
            y_values=speed_y,
        ),
        WaveformData(
            channel="CHANnel3",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(current_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=current_x,
            y_values=current_y,
        ),
    ]

    result = analyze_startup_brake_test(
        waveforms,
        StartupBrakeTestConfig(
            control_channel="CHANnel1",
            speed_channel="CHANnel2",
            current_channel="CHANnel3",
            speed_target_mode="period_ms",
            speed_target_value=3.0,
            speed_consecutive_periods=1,
            test_scope_mode="brake_only",
            brake_mode="current_zero",
            zero_current_threshold_a=0.5,
            zero_current_hold_s=0.002,
        ),
    )

    assert result.current_zero_window is not None
    assert result.current_zero_window.start_time_s >= 0.058


def _build_startup_brake_waveforms() -> list[WaveformData]:
    control_x = [index * 0.001 for index in range(101)]
    control_y = []
    for time_value in control_x:
        if time_value < 0.02:
            control_y.append(0.0)
        elif time_value < 0.08:
            control_y.append(10.0)
        else:
            control_y.append(0.0)

    speed_x = [index * 0.001 for index in range(101)]
    speed_y = []
    for time_value in speed_x:
        high = any(
            start <= time_value < (start + width)
            for start, width in (
                (0.02, 0.004),
                (0.03, 0.004),
                (0.04, 0.004),
                (0.05, 0.004),
                (0.06, 0.004),
                (0.08, 0.002),
                (0.085, 0.002),
                (0.091, 0.002),
            )
        )
        speed_y.append(5.0 if high else 0.0)

    current_x = [index * 0.001 for index in range(101)]
    current_y = []
    for time_value in current_x:
        if time_value < 0.012:
            current_y.append(0.0)
        elif time_value == 0.012:
            current_y.append(0.4)
        elif time_value == 0.013:
            current_y.append(0.9)
        elif time_value == 0.014:
            current_y.append(1.4)
        elif time_value == 0.015:
            current_y.append(1.8)
        elif time_value == 0.016:
            current_y.append(1.9)
        elif time_value == 0.017:
            current_y.append(1.7)
        elif time_value == 0.018:
            current_y.append(1.5)
        elif time_value == 0.019:
            current_y.append(1.8)
        elif time_value == 0.02:
            current_y.append(2.0)
        elif time_value < 0.072:
            current_y.append(0.8)
        elif time_value == 0.08:
            current_y.append(0.3)
        elif time_value == 0.081:
            current_y.append(0.2)
        elif time_value == 0.082:
            current_y.append(0.12)
        elif time_value == 0.083:
            current_y.append(0.07)
        elif time_value == 0.084:
            current_y.append(0.06)
        elif time_value == 0.085:
            current_y.append(0.051)
        elif time_value == 0.086:
            current_y.append(0.03)
        elif time_value == 0.087:
            current_y.append(0.01)
        else:
            current_y.append(0.0)

    encoder_x = [index * 0.0002 for index in range(451)]
    encoder_y = []
    for time_value in encoder_x:
        high = any(
            start <= time_value < (start + 0.0004)
            for start in (0.0850, 0.0854, 0.0858, 0.0862, 0.0866, 0.0870, 0.0874, 0.0878)
        )
        encoder_y.append(5.0 if high else 0.0)

    return [
        WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(control_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=control_x,
            y_values=control_y,
        ),
        WaveformData(
            channel="CHANnel2",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(speed_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=speed_x,
            y_values=speed_y,
        ),
        WaveformData(
            channel="CHANnel3",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(current_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=current_x,
            y_values=current_y,
        ),
        WaveformData(
            channel="CHANnel4",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(encoder_x), 1, 0.0002, 0.0, 0, 1.0, 0.0, 0),
            x_values=encoder_x,
            y_values=encoder_y,
        ),
    ]


def _build_false_brake_then_real_brake_waveforms() -> list[WaveformData]:
    control_x = [index * 0.001 for index in range(121)]
    control_y = []
    for time_value in control_x:
        high = (
            (0.02 <= time_value < 0.06)
            or (0.064 <= time_value < 0.068)
            or (0.07 <= time_value < 0.08)
        )
        control_y.append(10.0 if high else 0.0)

    speed_x = [index * 0.001 for index in range(121)]
    speed_y = []
    for time_value in speed_x:
        high = any(
            start <= time_value < (start + width)
            for start, width in (
                (0.02, 0.004),
                (0.03, 0.004),
                (0.04, 0.004),
                (0.05, 0.004),
                (0.06, 0.004),
                (0.07, 0.004),
                (0.08, 0.002),
                (0.085, 0.002),
                (0.091, 0.002),
            )
        )
        speed_y.append(5.0 if high else 0.0)

    current_x = [index * 0.001 for index in range(121)]
    current_y = []
    for time_value in current_x:
        if time_value < 0.02:
            current_y.append(0.0)
        elif time_value < 0.072:
            current_y.append(0.9)
        elif time_value == 0.08:
            current_y.append(0.32)
        elif time_value == 0.081:
            current_y.append(0.2)
        elif time_value == 0.082:
            current_y.append(0.12)
        elif time_value == 0.083:
            current_y.append(0.08)
        elif time_value == 0.084:
            current_y.append(0.05)
        elif time_value == 0.085:
            current_y.append(0.03)
        elif time_value == 0.086:
            current_y.append(0.01)
        else:
            current_y.append(0.0)

    return [
        WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(control_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=control_x,
            y_values=control_y,
        ),
        WaveformData(
            channel="CHANnel2",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(speed_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=speed_x,
            y_values=speed_y,
        ),
        WaveformData(
            channel="CHANnel3",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(current_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=current_x,
            y_values=current_y,
        ),
    ]


def _build_rippled_brake_control_waveforms() -> list[WaveformData]:
    control_x = [index * 0.001 for index in range(121)]
    control_y = []
    for time_value in control_x:
        if time_value < 0.02:
            control_y.append(1.9)
        elif time_value < 0.079:
            if abs(time_value - 0.03) < 1e-12 or abs(time_value - 0.045) < 1e-12 or abs(time_value - 0.06) < 1e-12:
                control_y.append(4.72)
            else:
                control_y.append(4.94)
        else:
            control_y.append(1.9)

    speed_x = [index * 0.001 for index in range(121)]
    speed_y = []
    for time_value in speed_x:
        high = any(
            start <= time_value < (start + width)
            for start, width in (
                (0.02, 0.004),
                (0.03, 0.004),
                (0.04, 0.004),
                (0.05, 0.004),
                (0.06, 0.004),
                (0.07, 0.004),
                (0.08, 0.002),
                (0.085, 0.002),
                (0.091, 0.002),
            )
        )
        speed_y.append(5.0 if high else 0.0)

    current_x = [index * 0.001 for index in range(121)]
    current_y = []
    for time_value in current_x:
        if time_value < 0.02:
            current_y.append(0.0)
        elif time_value < 0.079:
            current_y.append(0.8)
        elif time_value == 0.08:
            current_y.append(0.3)
        elif time_value == 0.081:
            current_y.append(0.2)
        elif time_value == 0.082:
            current_y.append(0.12)
        elif time_value == 0.083:
            current_y.append(0.07)
        elif time_value == 0.084:
            current_y.append(0.06)
        elif time_value == 0.085:
            current_y.append(0.051)
        elif time_value == 0.086:
            current_y.append(0.03)
        elif time_value == 0.087:
            current_y.append(0.01)
        else:
            current_y.append(0.0)

    return [
        WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(control_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=control_x,
            y_values=control_y,
        ),
        WaveformData(
            channel="CHANnel2",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(speed_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=speed_x,
            y_values=speed_y,
        ),
        WaveformData(
            channel="CHANnel3",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(current_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=current_x,
            y_values=current_y,
        ),
    ]


def _build_brake_control_with_short_low_rebound_waveforms() -> list[WaveformData]:
    control_x = [index * 0.001 for index in range(121)]
    control_y = []
    for time_value in control_x:
        if time_value < 0.02:
            control_y.append(1.9)
        elif time_value < 0.079:
            control_y.append(4.94)
        elif abs(time_value - 0.08) < 1e-12:
            control_y.append(2.2)
        elif abs(time_value - 0.081) < 1e-12:
            control_y.append(2.7)
        else:
            control_y.append(1.9)

    speed_x = [index * 0.001 for index in range(121)]
    speed_y = []
    for time_value in speed_x:
        high = any(
            start <= time_value < (start + width)
            for start, width in (
                (0.02, 0.004),
                (0.03, 0.004),
                (0.04, 0.004),
                (0.05, 0.004),
                (0.06, 0.004),
                (0.07, 0.004),
                (0.08, 0.002),
                (0.085, 0.002),
                (0.091, 0.002),
            )
        )
        speed_y.append(5.0 if high else 0.0)

    current_x = [index * 0.001 for index in range(121)]
    current_y = []
    for time_value in current_x:
        if time_value < 0.02:
            current_y.append(0.0)
        elif time_value < 0.079:
            current_y.append(0.8)
        elif time_value == 0.08:
            current_y.append(0.3)
        elif time_value == 0.081:
            current_y.append(0.2)
        elif time_value == 0.082:
            current_y.append(0.12)
        elif time_value == 0.083:
            current_y.append(0.07)
        elif time_value == 0.084:
            current_y.append(0.06)
        elif time_value == 0.085:
            current_y.append(0.051)
        elif time_value == 0.086:
            current_y.append(0.03)
        elif time_value == 0.087:
            current_y.append(0.01)
        else:
            current_y.append(0.0)

    return [
        WaveformData(
            channel="CHANnel1",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(control_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=control_x,
            y_values=control_y,
        ),
        WaveformData(
            channel="CHANnel2",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(speed_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=speed_x,
            y_values=speed_y,
        ),
        WaveformData(
            channel="CHANnel3",
            points_mode="NORMal",
            preamble=WaveformPreamble(0, 0, len(current_x), 1, 0.001, 0.0, 0, 1.0, 0.0, 0),
            x_values=current_x,
            y_values=current_y,
        ),
    ]
