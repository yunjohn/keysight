from keysight_scope_app.analysis.waveform import WaveformData, WaveformPreamble
from keysight_scope_app.services.batch import BatchRunner, CancellationToken
from keysight_scope_app.services.quality import inspect_waveforms
from keysight_scope_app.services.test_profiles import TestRun


def test_batch_keeps_successes_and_errors() -> None:
    def execute(sample_id: str, index: int) -> TestRun:
        if index == 2:
            raise RuntimeError("fixture failure")
        return TestRun(sample_id, "P", "1", ())

    result = BatchRunner().run("S1", 3, execute)
    assert len(result.runs) == 2
    assert len(result.errors) == 1


def test_batch_can_cancel_from_progress_callback() -> None:
    token = CancellationToken()

    def progress(completed: int, total: int) -> None:
        token.cancel()

    result = BatchRunner().run("S1", 4, lambda sample, index: TestRun(sample, "P", "1", ()),
                               cancellation=token, on_progress=progress)
    assert len(result.runs) == 1
    assert result.cancelled


def test_quality_reports_missing_and_invalid_time_axis() -> None:
    waveform = WaveformData(
        "CHANnel1",
        "NORMal",
        WaveformPreamble(0, 0, 3, 1, 1, 0, 0, 1, 0, 0),
        [0.0, 1.0, 1.0],
        [0.0, 1.0, 0.0],
    )
    codes = {issue.code for issue in inspect_waveforms([waveform], required_channels=("CHANnel2",), minimum_points=2)}
    assert {"missing_channel", "non_monotonic_time"} <= codes

