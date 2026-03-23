from pathlib import Path

from keysight_scope_app.instrument import KeysightOscilloscope, WaveformData, WaveformPreamble, compare_waveform_edges
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
