from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from keysight_scope_app.analysis.waveform import WaveformData


@dataclass(frozen=True)
class WaveformComparison:
    channel: str
    sample_count: int
    time_shift_s: float
    mean_error: float
    rms_error: float
    maximum_absolute_error: float
    differences: tuple[tuple[float, float], ...]


def compare_to_baseline(
    baseline: WaveformData,
    candidate: WaveformData,
    *,
    time_shift_s: float = 0.0,
) -> WaveformComparison:
    if not baseline.x_values or not candidate.x_values:
        raise ValueError("基准波形和待比较波形都必须包含数据。")
    differences: list[tuple[float, float]] = []
    errors: list[float] = []
    shifted_x = [value + time_shift_s for value in candidate.x_values]
    for time_value, baseline_value in zip(baseline.x_values, baseline.y_values):
        candidate_value = _interpolate(shifted_x, candidate.y_values, time_value)
        if candidate_value is None:
            continue
        error = candidate_value - baseline_value
        differences.append((time_value, error))
        errors.append(error)
    if not errors:
        raise ValueError("两条波形在应用时间对齐后没有重叠区间。")
    return WaveformComparison(
        channel=candidate.channel,
        sample_count=len(errors),
        time_shift_s=time_shift_s,
        mean_error=sum(errors) / len(errors),
        rms_error=math.sqrt(sum(value * value for value in errors) / len(errors)),
        maximum_absolute_error=max(abs(value) for value in errors),
        differences=tuple(differences),
    )


def _interpolate(x_values: list[float], y_values: list[float], target: float) -> float | None:
    if not x_values or len(x_values) != len(y_values) or target < x_values[0] or target > x_values[-1]:
        return None
    index = bisect.bisect_left(x_values, target)
    if index == 0:
        return y_values[0]
    if index == len(x_values):
        return y_values[-1]
    left_x, right_x = x_values[index - 1], x_values[index]
    if right_x <= left_x:
        raise ValueError("待比较波形时间轴必须严格递增。")
    ratio = (target - left_x) / (right_x - left_x)
    return y_values[index - 1] + ratio * (y_values[index] - y_values[index - 1])
