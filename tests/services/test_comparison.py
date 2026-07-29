import pytest

from keysight_scope_app.analysis.waveform import WaveformData, WaveformPreamble
from keysight_scope_app.services.comparison import compare_to_baseline


def _waveform(channel: str, values: list[float]) -> WaveformData:
    return WaveformData(
        channel,
        "NORMal",
        WaveformPreamble(0, 0, len(values), 1, 1.0, 0.0, 0, 1.0, 0.0, 0),
        [float(index) for index in range(len(values))],
        values,
    )


def test_baseline_comparison_reports_error_metrics() -> None:
    result = compare_to_baseline(_waveform("REF", [0, 1, 2]), _waveform("DUT", [1, 2, 3]))
    assert result.mean_error == pytest.approx(1.0)
    assert result.rms_error == pytest.approx(1.0)
    assert result.maximum_absolute_error == pytest.approx(1.0)
