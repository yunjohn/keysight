from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from keysight_scope_app.analysis.waveform import WaveformData


@dataclass(frozen=True)
class DataQualityIssue:
    code: str
    message: str
    channel: str | None = None
    severity: str = "warning"


def inspect_waveforms(
    waveforms: list[WaveformData] | tuple[WaveformData, ...],
    *,
    required_channels: tuple[str, ...] = (),
    minimum_points: int = 100,
) -> tuple[DataQualityIssue, ...]:
    issues: list[DataQualityIssue] = []
    by_channel = {waveform.channel: waveform for waveform in waveforms}
    for channel in required_channels:
        if channel not in by_channel:
            issues.append(DataQualityIssue("missing_channel", f"缺少必需通道 {channel}。", channel, "error"))
    for waveform in waveforms:
        point_count = min(len(waveform.x_values), len(waveform.y_values))
        if point_count < minimum_points:
            issues.append(
                DataQualityIssue("insufficient_samples", f"采样点不足（{point_count} < {minimum_points}）。", waveform.channel)
            )
        if len(waveform.x_values) != len(waveform.y_values):
            issues.append(DataQualityIssue("length_mismatch", "时间轴与采样值长度不一致。", waveform.channel, "error"))
        if any(not math.isfinite(value) for value in waveform.y_values):
            issues.append(DataQualityIssue("non_finite", "波形包含非有限值。", waveform.channel, "error"))
        if any(right <= left for left, right in zip(waveform.x_values, waveform.x_values[1:])):
            issues.append(DataQualityIssue("non_monotonic_time", "波形时间轴不是严格递增。", waveform.channel, "error"))
        if point_count >= 20:
            low, high = min(waveform.y_values), max(waveform.y_values)
            tolerance = max((high - low) * 1e-9, 1e-12)
            edge_hits = sum(
                abs(value - low) <= tolerance or abs(value - high) <= tolerance
                for value in waveform.y_values
            )
            if high > low and edge_hits / point_count >= 0.2:
                issues.append(DataQualityIssue("possible_clipping", "大量采样位于极值，可能存在削顶。", waveform.channel))
            differences = [
                waveform.y_values[index] - waveform.y_values[index - 1]
                for index in range(1, point_count)
            ]
            if differences and statistics.pstdev(differences) > max(high - low, 1e-12) * 0.5:
                issues.append(DataQualityIssue("high_noise", "相邻采样变化较大，分析可信度可能下降。", waveform.channel))
    return tuple(issues)
