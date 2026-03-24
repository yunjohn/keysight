from pathlib import Path

from keysight_scope_app.device.instrument import (
    KeysightOscilloscope,
    StartupBrakeTestConfig,
    WaveformData,
    WaveformPreamble,
    analyze_startup_brake_test,
    compare_waveform_edges,
)
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
    results = scope.fetch_measurements("CHANnel1", ["频率", "占空比", "正脉宽", "负脉宽", "下降时间", "高电平估计"])
    result_map = {result.label: result.raw_value for result in results}

    assert scope.waveform_fetch_count == 1
    assert result_map["频率"] == 1000.0
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
    assert abs(result.brake_start_point[0] - 0.0791) < 1e-9
    assert abs(result.current_zero_window.start_time_s - 0.086) < 1e-9
    assert abs(result.current_zero_window.confirmed_time_s - 0.089) < 1e-9
    assert abs(result.brake_end_point[0] - 0.086) < 1e-9
    assert abs(result.brake_delay_s - 0.0069) < 1e-9


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

    assert abs(result.current_zero_window.confirmed_time_s - 0.089) < 1e-9
    assert abs(result.brake_end_point[0] - 0.0849) < 1e-9
    assert abs(result.brake_delay_s - 0.0058) < 1e-9


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


def test_analyze_startup_brake_test_brake_start_uses_last_control_falling_edge_before_speed_zero() -> None:
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
    assert abs(result.brake_start_point[0] - 0.0791) < 1e-9


def test_analyze_startup_brake_test_current_zero_mode_accepts_probe_jitter_near_zero() -> None:
    waveforms = WaveformData.load_csv_bundle(Path("captures/waveforms/test.csv"))

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
    waveforms = WaveformData.load_csv_bundle(Path("captures/waveforms/test1.csv"))

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
