from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math
import threading
from typing import Callable

import pyvisa
from pyvisa.errors import VisaIOError

from keysight_scope_app.utils import format_engineering_value, strip_ieee4882_block


QueryBuilder = Callable[[str], str]
StatsValueGetter = Callable[["WaveformStats"], float | None]
BUNDLE_MAGIC = "# KEYSIGHT_SCOPE_BUNDLE_V1"
BUNDLE_SECTION_PREFIX = "# channel="


@dataclass(frozen=True)
class MeasurementDefinition:
    label: str
    unit: str
    query_builder: QueryBuilder | None = None
    stats_getter: StatsValueGetter | None = None


@dataclass(frozen=True)
class MeasurementResult:
    label: str
    raw_value: float
    unit: str
    display_value: str


@dataclass(frozen=True)
class WaveformPreamble:
    format_code: int
    acquire_type: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: int
    y_increment: float
    y_origin: float
    y_reference: int


@dataclass(frozen=True)
class WaveformData:
    channel: str
    points_mode: str
    preamble: WaveformPreamble
    x_values: list[float]
    y_values: list[float]

    def export_csv(self, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["time_s", "voltage_v"])
            for time_value, voltage_value in zip(self.x_values, self.y_values):
                writer.writerow([f"{time_value:.12e}", f"{voltage_value:.12e}"])
        return target_path

    @staticmethod
    def export_csv_bundle(waveforms: list["WaveformData"], target_path: Path) -> Path:
        if not waveforms:
            raise ValueError("没有可导出的波形数据。")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow([BUNDLE_MAGIC])
            for waveform in waveforms:
                writer.writerow([f"{BUNDLE_SECTION_PREFIX}{waveform.channel},points_mode={waveform.points_mode}"])
                writer.writerow(["time_s", "voltage_v"])
                for time_value, voltage_value in zip(waveform.x_values, waveform.y_values):
                    writer.writerow([f"{time_value:.12e}", f"{voltage_value:.12e}"])
                writer.writerow([])
        return target_path

    @classmethod
    def from_csv(cls, source_path: Path, channel: str = "CSV", points_mode: str = "FILE") -> "WaveformData":
        x_values: list[float] = []
        y_values: list[float] = []
        with source_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                x_values.append(float(row["time_s"]))
                y_values.append(float(row["voltage_v"]))

        if not x_values:
            raise ValueError(f"波形 CSV 没有数据: {source_path}")

        x_increment = x_values[1] - x_values[0] if len(x_values) >= 2 else 0.0
        y_min = min(y_values)
        return cls(
            channel=channel,
            points_mode=points_mode,
            preamble=WaveformPreamble(
                format_code=4,
                acquire_type=0,
                points=len(x_values),
                count=1,
                x_increment=x_increment,
                x_origin=x_values[0],
                x_reference=0,
                y_increment=0.0,
                y_origin=y_min,
                y_reference=0,
            ),
            x_values=x_values,
            y_values=y_values,
        )

    @classmethod
    def load_csv_bundle(cls, source_path: Path) -> list["WaveformData"]:
        with source_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.reader(csv_file)
            rows = [row for row in reader]

        if not rows:
            raise ValueError(f"波形 CSV 没有数据: {source_path}")

        first_cell = rows[0][0].strip() if rows[0] else ""
        if first_cell != BUNDLE_MAGIC:
            return [cls.from_csv(source_path, channel=source_path.stem, points_mode="FILE")]

        waveforms: list[WaveformData] = []
        index = 1
        while index < len(rows):
            row = rows[index]
            first_cell = row[0].strip() if row else ""
            if not first_cell:
                index += 1
                continue
            if not first_cell.startswith(BUNDLE_SECTION_PREFIX):
                raise ValueError(f"无效的多通道波形包格式: {source_path}")

            metadata = _parse_bundle_section_header(first_cell)
            index += 1
            if index >= len(rows) or [cell.strip() for cell in rows[index]] != ["time_s", "voltage_v"]:
                raise ValueError(f"波形包缺少表头: {source_path}")
            index += 1

            x_values: list[float] = []
            y_values: list[float] = []
            while index < len(rows):
                data_row = rows[index]
                first_value = data_row[0].strip() if data_row else ""
                if not first_value:
                    index += 1
                    break
                if first_value.startswith(BUNDLE_SECTION_PREFIX):
                    break
                if len(data_row) < 2:
                    raise ValueError(f"波形包数据列不足: {source_path}")
                x_values.append(float(data_row[0]))
                y_values.append(float(data_row[1]))
                index += 1

            if not x_values:
                raise ValueError(f"波形包分表没有数据: {metadata['channel']}")

            x_increment = x_values[1] - x_values[0] if len(x_values) >= 2 else 0.0
            y_min = min(y_values)
            waveforms.append(
                cls(
                    channel=metadata["channel"],
                    points_mode=metadata["points_mode"],
                    preamble=WaveformPreamble(
                        format_code=4,
                        acquire_type=0,
                        points=len(x_values),
                        count=1,
                        x_increment=x_increment,
                        x_origin=x_values[0],
                        x_reference=0,
                        y_increment=0.0,
                        y_origin=y_min,
                        y_reference=0,
                    ),
                    x_values=x_values,
                    y_values=y_values,
                )
            )

        return waveforms

    def analyze(self) -> "WaveformStats":
        if not self.x_values or not self.y_values:
            raise ValueError("波形数据为空。")

        point_count = min(len(self.x_values), len(self.y_values))
        x_values = self.x_values[:point_count]
        y_values = self.y_values[:point_count]
        voltage_min = min(y_values)
        voltage_max = max(y_values)
        voltage_mean = sum(y_values) / point_count
        voltage_rms = math.sqrt(sum(value * value for value in y_values) / point_count)
        duration = x_values[-1] - x_values[0] if point_count >= 2 else 0.0
        sample_period = duration / (point_count - 1) if point_count >= 2 else 0.0
        amplitude = voltage_max - voltage_min
        mid_threshold = (voltage_max + voltage_min) / 2
        low_threshold = voltage_min + amplitude * 0.1
        high_threshold = voltage_min + amplitude * 0.9
        high_samples = [value for value in y_values if value >= mid_threshold]
        low_samples = [value for value in y_values if value < mid_threshold]
        logic_high_v = sum(high_samples) / len(high_samples) if high_samples else voltage_max
        logic_low_v = sum(low_samples) / len(low_samples) if low_samples else voltage_min
        amplitude_v = (logic_high_v - logic_low_v) / 2

        rising_mid_crossings = _find_crossings(x_values, y_values, mid_threshold, "rising")
        falling_mid_crossings = _find_crossings(x_values, y_values, mid_threshold, "falling")
        estimated_frequency_hz = _estimate_frequency_from_crossings(rising_mid_crossings)
        pulse_width_s = _estimate_pulse_width(rising_mid_crossings, falling_mid_crossings)
        duty_cycle = _estimate_duty_cycle(pulse_width_s, estimated_frequency_hz)
        rise_time_s = _estimate_transition_time(
            _find_crossings(x_values, y_values, low_threshold, "rising"),
            _find_crossings(x_values, y_values, high_threshold, "rising"),
        )
        fall_time_s = _estimate_transition_time(
            _find_crossings(x_values, y_values, high_threshold, "falling"),
            _find_crossings(x_values, y_values, low_threshold, "falling"),
        )
        return WaveformStats(
            point_count=point_count,
            duration_s=duration,
            sample_period_s=sample_period,
            voltage_min=voltage_min,
            voltage_max=voltage_max,
            voltage_pp=voltage_max - voltage_min,
            voltage_mean=voltage_mean,
            voltage_rms=voltage_rms,
            logic_low_v=logic_low_v,
            logic_high_v=logic_high_v,
            amplitude_v=amplitude_v,
            estimated_frequency_hz=estimated_frequency_hz,
            pulse_width_s=pulse_width_s,
            duty_cycle=duty_cycle,
            rise_time_s=rise_time_s,
            fall_time_s=fall_time_s,
        )

    def slice_by_time(self, start_x: float, end_x: float) -> "WaveformData" | None:
        if not self.x_values or not self.y_values:
            return None

        left = min(start_x, end_x)
        right = max(start_x, end_x)
        sliced_x: list[float] = []
        sliced_y: list[float] = []
        for x_value, y_value in zip(self.x_values, self.y_values):
            if left <= x_value <= right:
                sliced_x.append(x_value)
                sliced_y.append(y_value)

        if len(sliced_x) < 2:
            return None

        x_increment = sliced_x[1] - sliced_x[0]
        y_min = min(sliced_y)
        return WaveformData(
            channel=self.channel,
            points_mode=self.points_mode,
            preamble=WaveformPreamble(
                format_code=self.preamble.format_code,
                acquire_type=self.preamble.acquire_type,
                points=len(sliced_x),
                count=self.preamble.count,
                x_increment=x_increment,
                x_origin=sliced_x[0],
                x_reference=0,
                y_increment=self.preamble.y_increment,
                y_origin=y_min if self.preamble.y_increment == 0 else self.preamble.y_origin,
                y_reference=self.preamble.y_reference if self.preamble.y_increment != 0 else 0,
            ),
            x_values=sliced_x,
            y_values=sliced_y,
        )

    def analyze_window(self, start_x: float, end_x: float) -> "WaveformStats" | None:
        sliced = self.slice_by_time(start_x, end_x)
        if sliced is None:
            return None
        return sliced.analyze()

    def find_first_edge(
        self,
        edge_type: str,
        *,
        threshold_ratio: float = 0.5,
        start_time: float | None = None,
    ) -> tuple[float, float] | None:
        if not self.x_values or not self.y_values:
            return None
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的边沿类型: {edge_type}")

        threshold = _edge_threshold(self.y_values, edge_type=edge_type, threshold_ratio=threshold_ratio)
        crossings = _find_crossings(self.x_values, self.y_values, threshold, edge_type)
        for crossing in crossings:
            if start_time is None or crossing >= start_time:
                return crossing, threshold
        return None

    def snap_to_edge(self, x_hint: float, edge_type: str) -> tuple[float, float] | None:
        if not self.x_values or not self.y_values:
            return None
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的边沿类型: {edge_type}")

        voltage_min = min(self.y_values)
        voltage_max = max(self.y_values)
        threshold = (voltage_max + voltage_min) / 2
        crossings = _find_crossings(self.x_values, self.y_values, threshold, edge_type)
        if not crossings:
            return None

        target_x = min(crossings, key=lambda crossing: abs(crossing - x_hint))
        return target_x, threshold

    def find_nearest_pulse(self, x_hint: float) -> "PulseWindow" | None:
        if not self.x_values or not self.y_values:
            return None

        voltage_min = min(self.y_values)
        voltage_max = max(self.y_values)
        threshold = (voltage_max + voltage_min) / 2
        rising_crossings = _find_crossings(self.x_values, self.y_values, threshold, "rising")
        falling_crossings = _find_crossings(self.x_values, self.y_values, threshold, "falling")
        pulses = _find_pulses(rising_crossings, falling_crossings, threshold)
        if not pulses:
            return None
        return min(
            pulses,
            key=lambda pulse: abs(((pulse.rising_edge[0] + pulse.falling_edge[0]) / 2) - x_hint),
        )

    def find_nearest_period(self, x_hint: float, edge_type: str = "rising") -> "PeriodWindow" | None:
        if not self.x_values or not self.y_values:
            return None
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的周期边沿类型: {edge_type}")

        voltage_min = min(self.y_values)
        voltage_max = max(self.y_values)
        threshold = (voltage_max + voltage_min) / 2
        crossings = _find_crossings(self.x_values, self.y_values, threshold, edge_type)
        periods = _find_periods(crossings, threshold, edge_type)
        if not periods:
            return None
        return min(
            periods,
            key=lambda period: abs(((period.start_edge[0] + period.end_edge[0]) / 2) - x_hint),
        )

    def recommend_lock_window(self, x_hint: float) -> "LockRecommendation" | None:
        candidates: list[LockRecommendation] = []

        for edge_type, description in (
            ("rising", "已锁定最近完整周期，A/B 游标已对齐到相邻上升沿。"),
            ("falling", "已锁定最近完整周期，A/B 游标已对齐到相邻下降沿。"),
        ):
            period = self.find_nearest_period(x_hint, edge_type=edge_type)
            if period is not None:
                candidates.append(
                    LockRecommendation(
                        mode="period",
                        start_edge=period.start_edge,
                        end_edge=period.end_edge,
                        description=description,
                    )
                )

        pulse = self.find_nearest_pulse(x_hint)
        if pulse is not None:
            candidates.append(
                LockRecommendation(
                    mode="pulse",
                    start_edge=pulse.rising_edge,
                    end_edge=pulse.falling_edge,
                    description="已锁定最近完整脉冲，A/B 游标已自动对齐。",
                )
            )

        if not candidates:
            return None

        period_candidates = [candidate for candidate in candidates if candidate.mode == "period"]
        ranked_candidates = period_candidates or candidates
        return min(
            ranked_candidates,
            key=lambda candidate: abs(((candidate.start_edge[0] + candidate.end_edge[0]) / 2) - x_hint),
        )

    def find_target_cycle(
        self,
        *,
        target_mode: str,
        target_value: float,
        tolerance_ratio: float = 0.05,
        consecutive_periods: int = 3,
        start_time: float = 0.0,
        pulses_per_revolution: int = 1,
        edge_type: str = "rising",
    ) -> "SpeedTargetMatch" | None:
        if not self.x_values or not self.y_values:
            return None
        if target_value <= 0:
            raise ValueError("目标值必须大于 0。")
        if tolerance_ratio < 0:
            raise ValueError("容差比例不能为负数。")
        if consecutive_periods <= 0:
            raise ValueError("连续周期数必须大于 0。")
        if pulses_per_revolution <= 0:
            raise ValueError("每转脉冲数必须大于 0。")
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的周期边沿类型: {edge_type}")

        threshold = _edge_threshold(self.y_values, edge_type=edge_type, threshold_ratio=0.5)
        crossings = _find_crossings(self.x_values, self.y_values, threshold, edge_type)
        streak = 0
        for crossing_index in range(1, len(crossings)):
            previous_edge = crossings[crossing_index - 1]
            current_edge = crossings[crossing_index]
            if current_edge <= previous_edge:
                continue
            if current_edge < start_time:
                streak = 0
                continue

            period_s = current_edge - previous_edge
            frequency_hz = 1.0 / period_s if period_s > 0 else 0.0
            rpm = (frequency_hz * 60.0) / pulses_per_revolution
            if _matches_target_cycle(
                target_mode=target_mode,
                target_value=target_value,
                period_s=period_s,
                frequency_hz=frequency_hz,
                rpm=rpm,
                tolerance_ratio=tolerance_ratio,
            ):
                streak += 1
                if streak < consecutive_periods:
                    continue

                first_period_index = crossing_index - consecutive_periods + 1
                period_values = [
                    crossings[index] - crossings[index - 1]
                    for index in range(first_period_index, crossing_index + 1)
                ]
                average_period_s = sum(period_values) / len(period_values)
                average_frequency_hz = 1.0 / average_period_s if average_period_s > 0 else 0.0
                average_rpm = (average_frequency_hz * 60.0) / pulses_per_revolution
                return SpeedTargetMatch(
                    edge_type=edge_type,
                    start_time_s=crossings[first_period_index - 1],
                    reached_time_s=current_edge,
                    period_s=average_period_s,
                    frequency_hz=average_frequency_hz,
                    rpm=average_rpm,
                    threshold=threshold,
                    matched_cycles=consecutive_periods,
                )
            streak = 0
        return None

    def find_zero_stable_window(
        self,
        *,
        start_time: float = 0.0,
        zero_threshold: float = 0.05,
        flat_threshold: float = 0.03,
        hold_time_s: float = 0.002,
    ) -> "ZeroStableWindow" | None:
        if not self.x_values or not self.y_values:
            return None
        if zero_threshold < 0:
            raise ValueError("零电流阈值不能为负数。")
        if flat_threshold < 0:
            raise ValueError("水平线波动阈值不能为负数。")
        if hold_time_s < 0:
            raise ValueError("保持时间不能为负数。")

        point_count = min(len(self.x_values), len(self.y_values))
        start_index = 0
        while start_index < point_count and self.x_values[start_index] < start_time:
            start_index += 1

        for left in range(start_index, point_count):
            left_value = self.y_values[left]
            if abs(left_value) > zero_threshold:
                continue

            min_value = left_value
            max_value = left_value
            max_abs_value = abs(left_value)
            for right in range(left, point_count):
                value = self.y_values[right]
                min_value = min(min_value, value)
                max_value = max(max_value, value)
                max_abs_value = max(max_abs_value, abs(value))
                if max_abs_value > zero_threshold or (max_value - min_value) > flat_threshold:
                    break
                duration_s = self.x_values[right] - self.x_values[left]
                if duration_s >= hold_time_s:
                    return ZeroStableWindow(
                        start_time_s=self.x_values[left],
                        confirmed_time_s=self.x_values[right],
                        max_abs_value=max_abs_value,
                        span_value=max_value - min_value,
                    )
        return None

    def find_previous_edge(
        self,
        reference_time: float,
        *,
        count: int = 1,
        edge_type: str = "rising",
    ) -> tuple[float, float] | None:
        if not self.x_values or not self.y_values:
            return None
        if count <= 0:
            raise ValueError("回溯脉冲数必须大于 0。")
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的边沿类型: {edge_type}")

        threshold = _edge_threshold(self.y_values, edge_type=edge_type, threshold_ratio=0.5)
        crossings = [crossing for crossing in _find_crossings(self.x_values, self.y_values, threshold, edge_type) if crossing < reference_time]
        if len(crossings) < count:
            return None
        return crossings[-count], threshold

    def peak_absolute_between(self, start_x: float, end_x: float) -> "SignalPeak" | None:
        if not self.x_values or not self.y_values:
            return None

        left = min(start_x, end_x)
        right = max(start_x, end_x)
        candidates = [
            (time_value, signal_value)
            for time_value, signal_value in zip(self.x_values, self.y_values)
            if left <= time_value <= right
        ]
        if not candidates:
            return None

        peak_time, peak_value = max(candidates, key=lambda item: (abs(item[1]), -item[0]))
        return SignalPeak(time_s=peak_time, value=peak_value, absolute_value=abs(peak_value))


@dataclass(frozen=True)
class WaveformStats:
    point_count: int
    duration_s: float
    sample_period_s: float
    voltage_min: float
    voltage_max: float
    voltage_pp: float
    voltage_mean: float
    voltage_rms: float
    logic_low_v: float
    logic_high_v: float
    amplitude_v: float
    estimated_frequency_hz: float | None
    pulse_width_s: float | None
    duty_cycle: float | None
    rise_time_s: float | None
    fall_time_s: float | None


@dataclass(frozen=True)
class PulseWindow:
    rising_edge: tuple[float, float]
    falling_edge: tuple[float, float]


@dataclass(frozen=True)
class PeriodWindow:
    start_edge: tuple[float, float]
    end_edge: tuple[float, float]
    edge_type: str


@dataclass(frozen=True)
class LockRecommendation:
    mode: str
    start_edge: tuple[float, float]
    end_edge: tuple[float, float]
    description: str


@dataclass(frozen=True)
class EdgeComparison:
    edge_type: str
    primary_time_s: float
    secondary_time_s: float
    delta_t_s: float
    frequency_hz: float | None
    phase_deg: float | None


@dataclass(frozen=True)
class SpeedTargetMatch:
    edge_type: str
    start_time_s: float
    reached_time_s: float
    period_s: float
    frequency_hz: float
    rpm: float
    threshold: float
    matched_cycles: int


@dataclass(frozen=True)
class ZeroStableWindow:
    start_time_s: float
    confirmed_time_s: float
    max_abs_value: float
    span_value: float


@dataclass(frozen=True)
class SignalPeak:
    time_s: float
    value: float
    absolute_value: float


@dataclass(frozen=True)
class StartupBrakeTestConfig:
    control_channel: str
    speed_channel: str
    current_channel: str
    encoder_a_channel: str | None = None
    speed_target_mode: str = "frequency_hz"
    speed_target_value: float = 0.0
    speed_tolerance_ratio: float = 0.05
    speed_consecutive_periods: int = 3
    pulses_per_revolution: int = 1
    control_threshold_ratio: float = 0.1
    zero_current_threshold_a: float = 0.05
    zero_current_flat_threshold_a: float = 0.03
    zero_current_hold_s: float = 0.002
    brake_mode: str = "current_zero"
    brake_backtrack_pulses: int = 8


@dataclass(frozen=True)
class StartupBrakeTestResult:
    startup_start_point: tuple[float, float]
    speed_reached_point: tuple[float, float]
    startup_delay_s: float
    startup_peak_current: SignalPeak | None
    speed_match: SpeedTargetMatch
    brake_start_point: tuple[float, float]
    current_zero_window: ZeroStableWindow
    brake_end_point: tuple[float, float]
    brake_delay_s: float
    brake_peak_current: SignalPeak | None
    brake_mode: str


MEASUREMENT_DEFINITIONS: dict[str, MeasurementDefinition] = {
    "频率": MeasurementDefinition("频率", "Hz", lambda channel: f":MEASure:FREQuency? {channel}"),
    "周期": MeasurementDefinition("周期", "s", lambda channel: f":MEASure:PERiod? {channel}"),
    "峰峰值": MeasurementDefinition("峰峰值", "V", lambda channel: f":MEASure:VPP? {channel}"),
    "均方根": MeasurementDefinition("均方根", "V", lambda channel: f":MEASure:VRMS? DISPlay,DC,{channel}"),
    "最大值": MeasurementDefinition("最大值", "V", lambda channel: f":MEASure:VMAX? {channel}"),
    "最小值": MeasurementDefinition("最小值", "V", lambda channel: f":MEASure:VMIN? {channel}"),
    "上升时间": MeasurementDefinition("上升时间", "s", lambda channel: f":MEASure:RISetime? {channel}"),
    "平均值": MeasurementDefinition("平均值", "V", stats_getter=lambda stats: stats.voltage_mean),
    "振幅": MeasurementDefinition("振幅", "V", stats_getter=lambda stats: stats.amplitude_v),
    "占空比": MeasurementDefinition("占空比", "%", stats_getter=lambda stats: _ratio_to_percent(stats.duty_cycle)),
    "正脉宽": MeasurementDefinition("正脉宽", "s", stats_getter=lambda stats: stats.pulse_width_s),
    "负脉宽": MeasurementDefinition("负脉宽", "s", stats_getter=lambda stats: _negative_pulse_width_from_stats(stats)),
    "高电平时间": MeasurementDefinition("高电平时间", "s", stats_getter=lambda stats: stats.pulse_width_s),
    "低电平时间": MeasurementDefinition("低电平时间", "s", stats_getter=lambda stats: _negative_pulse_width_from_stats(stats)),
    "下降时间": MeasurementDefinition("下降时间", "s", stats_getter=lambda stats: stats.fall_time_s),
    "高电平估计": MeasurementDefinition("高电平估计", "V", stats_getter=lambda stats: stats.logic_high_v),
    "低电平估计": MeasurementDefinition("低电平估计", "V", stats_getter=lambda stats: stats.logic_low_v),
}


SUPPORTED_CHANNELS = ("CHANnel1", "CHANnel2", "CHANnel3", "CHANnel4")
SUPPORTED_WAVEFORM_POINTS_MODES = ("NORMal", "MAXimum", "RAW")
KNOWN_KEYSIGHT_VENDORS = ("KEYSIGHT", "AGILENT")
SOFTWARE_MEASUREMENT_POINTS = 2000


def list_visa_resources(backend: str | None = None) -> tuple[str, ...]:
    resource_manager = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
    try:
        resources = tuple(resource_manager.list_resources())
        return tuple(sorted(resources, key=_resource_sort_key))
    finally:
        resource_manager.close()


class KeysightOscilloscope:
    def __init__(self, resource_name: str, backend: str | None = None, timeout_ms: int = 10000) -> None:
        self.resource_name = resource_name
        self.backend = backend or None
        self.timeout_ms = timeout_ms
        self._resource_manager: pyvisa.ResourceManager | None = None
        self._instrument = None
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        return self._instrument is not None

    def connect(self) -> str:
        with self._lock:
            if self._instrument is not None:
                return self.query("*IDN?")

            self._resource_manager = pyvisa.ResourceManager(self.backend) if self.backend else pyvisa.ResourceManager()
            resource_name = self._resolve_resource_name(self.resource_name)
            instrument = self._resource_manager.open_resource(resource_name)
            instrument.timeout = self.timeout_ms
            instrument.chunk_size = 1024 * 1024
            instrument.write_termination = "\n"
            instrument.read_termination = "\n"
            try:
                instrument.clear()
            except Exception:
                pass
            instrument.write("*CLS")
            self._instrument = instrument
            self.resource_name = resource_name
            idn = self.query("*IDN?")
            return idn

    def disconnect(self) -> None:
        with self._lock:
            instrument = self._instrument
            resource_manager = self._resource_manager
            self._instrument = None
            self._resource_manager = None

            if instrument is not None:
                instrument.close()
            if resource_manager is not None:
                resource_manager.close()

    def query(self, command: str) -> str:
        with self._lock:
            self._ensure_connected()
            return str(self._instrument.query(command)).strip()

    def write(self, command: str) -> None:
        with self._lock:
            self._ensure_connected()
            self._instrument.write(command)

    def autoscale(self) -> None:
        self.write(":AUToscale")

    def single(self) -> None:
        self.write(":SINGle")

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def get_system_error(self) -> str:
        return self.query(":SYSTem:ERRor?")

    def fetch_measurements(self, channel: str, measurement_names: list[str]) -> list[MeasurementResult]:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")

        results: list[MeasurementResult] = []
        waveform_stats: WaveformStats | None = None
        for measurement_name in measurement_names:
            definition = MEASUREMENT_DEFINITIONS[measurement_name]
            if definition.query_builder is not None:
                raw_text = self.query(definition.query_builder(channel))
                raw_value = float(raw_text)
            else:
                if definition.stats_getter is None:
                    raise ValueError(f"测量项定义不完整: {measurement_name}")
                if waveform_stats is None:
                    waveform_stats = self.fetch_waveform(
                        channel,
                        points_mode="NORMal",
                        points=SOFTWARE_MEASUREMENT_POINTS,
                    ).analyze()
                value = definition.stats_getter(waveform_stats)
                raw_value = value if value is not None else float("nan")
            results.append(
                MeasurementResult(
                    label=definition.label,
                    raw_value=raw_value,
                    unit=definition.unit,
                    display_value=format_engineering_value(raw_value, definition.unit),
                )
            )
        return results

    def capture_screenshot(self, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._ensure_connected()
            self._instrument.write(":HARDcopy:INKSaver OFF")
            try:
                payload = bytes(
                    self._instrument.query_binary_values(
                        ":DISPlay:DATA? PNG, COLor",
                        datatype="B",
                        container=bytearray,
                        header_fmt="ieee",
                        expect_termination=False,
                    )
                )
            except Exception:
                self._instrument.write(":DISPlay:DATA? PNG, COLor")
                payload = strip_ieee4882_block(self._instrument.read_raw())

        target_path.write_bytes(payload)
        return target_path

    def fetch_waveform(
        self,
        channel: str,
        *,
        points_mode: str = "NORMal",
        points: int = 1000,
    ) -> WaveformData:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")
        if points_mode not in SUPPORTED_WAVEFORM_POINTS_MODES:
            raise ValueError(f"不支持的波形点模式: {points_mode}")
        if points <= 0:
            raise ValueError("波形点数必须大于 0。")

        with self._lock:
            self._ensure_connected()
            self._instrument.write(f":WAVeform:SOURce {channel}")
            self._instrument.write(":WAVeform:FORMat BYTE")
            self._instrument.write(":WAVeform:UNSigned ON")
            self._instrument.write(f":WAVeform:POINts:MODE {points_mode}")
            self._instrument.write(f":WAVeform:POINts {points}")
            preamble_values = self._instrument.query_ascii_values(":WAVeform:PREamble?")
            payload = list(
                self._instrument.query_binary_values(
                    ":WAVeform:DATA?",
                    datatype="B",
                    container=list,
                    header_fmt="ieee",
                    expect_termination=False,
                )
            )

        preamble = _parse_preamble(preamble_values)
        x_values = [
            ((index - preamble.x_reference) * preamble.x_increment) + preamble.x_origin
            for index in range(len(payload))
        ]
        y_values = [
            ((sample - preamble.y_reference) * preamble.y_increment) + preamble.y_origin
            for sample in payload
        ]
        return WaveformData(
            channel=channel,
            points_mode=points_mode,
            preamble=preamble,
            x_values=x_values,
            y_values=y_values,
        )

    def assert_keysight_vendor(self) -> str:
        idn = self.query("*IDN?")
        if not any(vendor in idn.upper() for vendor in KNOWN_KEYSIGHT_VENDORS):
            raise RuntimeError(f"当前设备不是 Keysight/Agilent 示波器: {idn}")
        return idn

    def _ensure_connected(self) -> None:
        if self._instrument is None:
            raise RuntimeError("示波器尚未连接。")

    def _resolve_resource_name(self, resource_name: str) -> str:
        if "::?::" not in resource_name:
            return resource_name

        try:
            candidates = tuple(self._resource_manager.list_resources())
        except VisaIOError:
            return resource_name

        wildcard_parts = resource_name.split("::")
        for candidate in sorted(candidates, key=_resource_sort_key):
            candidate_parts = candidate.split("::")
            if len(candidate_parts) != len(wildcard_parts):
                continue
            if all(expected == "?" or expected == actual for expected, actual in zip(wildcard_parts, candidate_parts)):
                return candidate
        return resource_name


def _resource_sort_key(resource_name: str) -> tuple[int, str]:
    return ("::?::" in resource_name, resource_name)


def _parse_preamble(values: list[float]) -> WaveformPreamble:
    if len(values) < 10:
        raise ValueError(f"波形前导信息长度异常: {values}")
    return WaveformPreamble(
        format_code=int(values[0]),
        acquire_type=int(values[1]),
        points=int(values[2]),
        count=int(values[3]),
        x_increment=float(values[4]),
        x_origin=float(values[5]),
        x_reference=int(values[6]),
        y_increment=float(values[7]),
        y_origin=float(values[8]),
        y_reference=int(values[9]),
    )


def _find_crossings(x_values: list[float], y_values: list[float], threshold: float, edge_type: str) -> list[float]:
    crossing_times: list[float] = []
    for index in range(1, min(len(x_values), len(y_values))):
        previous_value = y_values[index - 1]
        current_value = y_values[index]
        is_crossing = (
            edge_type == "rising" and previous_value < threshold <= current_value
        ) or (
            edge_type == "falling" and previous_value > threshold >= current_value
        )
        if is_crossing:
            delta_voltage = current_value - previous_value
            if delta_voltage == 0:
                crossing_times.append(x_values[index])
                continue
            ratio = (threshold - previous_value) / delta_voltage
            crossing_time = x_values[index - 1] + ratio * (x_values[index] - x_values[index - 1])
            crossing_times.append(crossing_time)
    return crossing_times


def _estimate_frequency_from_crossings(crossing_times: list[float]) -> float | None:
    if len(crossing_times) < 2:
        return None

    periods = [
        crossing_times[index] - crossing_times[index - 1]
        for index in range(1, len(crossing_times))
        if crossing_times[index] > crossing_times[index - 1]
    ]
    if not periods:
        return None

    average_period = sum(periods) / len(periods)
    if average_period <= 0:
        return None
    return 1.0 / average_period


def _estimate_pulse_width(rising_crossings: list[float], falling_crossings: list[float]) -> float | None:
    widths: list[float] = []
    falling_index = 0
    for rising_time in rising_crossings:
        while falling_index < len(falling_crossings) and falling_crossings[falling_index] <= rising_time:
            falling_index += 1
        if falling_index >= len(falling_crossings):
            break
        widths.append(falling_crossings[falling_index] - rising_time)
        falling_index += 1
    if not widths:
        return None
    return sum(widths) / len(widths)


def _find_pulses(
    rising_crossings: list[float],
    falling_crossings: list[float],
    threshold: float,
) -> list[PulseWindow]:
    pulses: list[PulseWindow] = []
    falling_index = 0
    for rising_time in rising_crossings:
        while falling_index < len(falling_crossings) and falling_crossings[falling_index] <= rising_time:
            falling_index += 1
        if falling_index >= len(falling_crossings):
            break
        pulses.append(
            PulseWindow(
                rising_edge=(rising_time, threshold),
                falling_edge=(falling_crossings[falling_index], threshold),
            )
        )
        falling_index += 1
    return pulses


def _find_periods(crossings: list[float], threshold: float, edge_type: str) -> list[PeriodWindow]:
    periods: list[PeriodWindow] = []
    for index in range(1, len(crossings)):
        current = crossings[index - 1]
        following = crossings[index]
        if following <= current:
            continue
        periods.append(
            PeriodWindow(
                start_edge=(current, threshold),
                end_edge=(following, threshold),
                edge_type=edge_type,
            )
        )
    return periods


def _estimate_duty_cycle(pulse_width_s: float | None, frequency_hz: float | None) -> float | None:
    if pulse_width_s is None or frequency_hz is None or frequency_hz <= 0:
        return None
    period = 1.0 / frequency_hz
    if period <= 0:
        return None
    return pulse_width_s / period


def _ratio_to_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100.0


def _period_from_stats(stats: WaveformStats) -> float | None:
    if stats.estimated_frequency_hz is None or stats.estimated_frequency_hz <= 0:
        return None
    return 1.0 / stats.estimated_frequency_hz


def _negative_pulse_width_from_stats(stats: WaveformStats) -> float | None:
    period_s = _period_from_stats(stats)
    if period_s is None or stats.pulse_width_s is None:
        return None
    return max(period_s - stats.pulse_width_s, 0.0)


def _estimate_transition_time(start_crossings: list[float], end_crossings: list[float]) -> float | None:
    durations: list[float] = []
    end_index = 0
    for start_time in start_crossings:
        while end_index < len(end_crossings) and end_crossings[end_index] <= start_time:
            end_index += 1
        if end_index >= len(end_crossings):
            break
        durations.append(end_crossings[end_index] - start_time)
        end_index += 1
    if not durations:
        return None
    return sum(durations) / len(durations)


def _edge_threshold(y_values: list[float], *, edge_type: str, threshold_ratio: float) -> float:
    if not y_values:
        raise ValueError("波形数据为空。")
    if not 0 <= threshold_ratio <= 1:
        raise ValueError("阈值比例必须位于 0 到 1 之间。")

    voltage_min = min(y_values)
    voltage_max = max(y_values)
    amplitude = voltage_max - voltage_min
    if edge_type == "rising":
        return voltage_min + amplitude * threshold_ratio
    if edge_type == "falling":
        return voltage_max - amplitude * threshold_ratio
    raise ValueError(f"不支持的边沿类型: {edge_type}")


def _matches_target_cycle(
    *,
    target_mode: str,
    target_value: float,
    period_s: float,
    frequency_hz: float,
    rpm: float,
    tolerance_ratio: float,
) -> bool:
    if target_mode == "frequency_hz":
        target_measure = target_value
        actual_measure = frequency_hz
    elif target_mode == "period_s":
        target_measure = target_value
        actual_measure = period_s
    elif target_mode == "rpm":
        target_measure = target_value
        actual_measure = rpm
    else:
        raise ValueError(f"不支持的目标类型: {target_mode}")

    tolerance = abs(target_measure) * tolerance_ratio
    return abs(actual_measure - target_measure) <= tolerance


def compare_waveform_edges(
    primary: WaveformData,
    secondary: WaveformData,
    x_hint: float,
    edge_type: str,
    *,
    frequency_hz: float | None = None,
) -> EdgeComparison | None:
    primary_edge = primary.snap_to_edge(x_hint, edge_type)
    secondary_edge = secondary.snap_to_edge(x_hint, edge_type)
    if primary_edge is None or secondary_edge is None:
        return None

    if frequency_hz is None or frequency_hz <= 0:
        frequency_hz = primary.analyze().estimated_frequency_hz
    delta_t_s = secondary_edge[0] - primary_edge[0]
    phase_deg = _normalize_phase_degrees(delta_t_s * frequency_hz * 360.0) if frequency_hz and frequency_hz > 0 else None
    return EdgeComparison(
        edge_type=edge_type,
        primary_time_s=primary_edge[0],
        secondary_time_s=secondary_edge[0],
        delta_t_s=delta_t_s,
        frequency_hz=frequency_hz,
        phase_deg=phase_deg,
    )


def analyze_startup_brake_test(
    waveforms: list[WaveformData],
    config: StartupBrakeTestConfig,
) -> StartupBrakeTestResult:
    waveform_map = {waveform.channel: waveform for waveform in waveforms}
    missing_channels = [
        channel
        for channel in (
            config.control_channel,
            config.speed_channel,
            config.current_channel,
            config.encoder_a_channel,
        )
        if channel and channel not in waveform_map
    ]
    if missing_channels:
        raise ValueError(f"缺少测试所需通道波形: {', '.join(missing_channels)}")

    if config.speed_target_value <= 0:
        raise ValueError("目标转速/频率/周期必须大于 0。")
    if config.brake_mode not in {"current_zero", "encoder_backtrack"}:
        raise ValueError(f"不支持的刹车模式: {config.brake_mode}")

    control_waveform = waveform_map[config.control_channel]
    speed_waveform = waveform_map[config.speed_channel]
    current_waveform = waveform_map[config.current_channel]

    startup_start = control_waveform.find_first_edge(
        "rising",
        threshold_ratio=config.control_threshold_ratio,
    )
    if startup_start is None:
        raise ValueError("未检测到控制器启动上升沿。")

    speed_target_value = config.speed_target_value
    speed_target_mode = config.speed_target_mode
    if speed_target_mode == "period_ms":
        speed_target_mode = "period_s"
        speed_target_value = config.speed_target_value / 1000.0

    speed_match = speed_waveform.find_target_cycle(
        target_mode=speed_target_mode,
        target_value=speed_target_value,
        tolerance_ratio=config.speed_tolerance_ratio,
        consecutive_periods=config.speed_consecutive_periods,
        start_time=startup_start[0],
        pulses_per_revolution=config.pulses_per_revolution,
        edge_type="rising",
    )
    if speed_match is None:
        raise ValueError("未检测到达到目标转速的连续脉冲窗口。")

    speed_reached_point = (speed_match.reached_time_s, speed_match.threshold)
    startup_peak_current = current_waveform.peak_absolute_between(startup_start[0], speed_match.reached_time_s)

    brake_start = control_waveform.find_first_edge(
        "falling",
        threshold_ratio=config.control_threshold_ratio,
        start_time=startup_start[0],
    )
    if brake_start is None:
        raise ValueError("未检测到控制器刹车下降沿。")

    current_zero_window = current_waveform.find_zero_stable_window(
        start_time=brake_start[0],
        zero_threshold=config.zero_current_threshold_a,
        flat_threshold=config.zero_current_flat_threshold_a,
        hold_time_s=config.zero_current_hold_s,
    )
    if current_zero_window is None:
        raise ValueError("未检测到满足阈值条件的零电流稳定区间。")

    if config.brake_mode == "current_zero":
        brake_end_point = (current_zero_window.confirmed_time_s, 0.0)
    else:
        if not config.encoder_a_channel:
            raise ValueError("A 相回溯模式需要选择编码器 A 相通道。")
        encoder_waveform = waveform_map[config.encoder_a_channel]
        brake_end_point = encoder_waveform.find_previous_edge(
            current_zero_window.confirmed_time_s,
            count=config.brake_backtrack_pulses,
            edge_type="rising",
        )
        if brake_end_point is None:
            raise ValueError(f"在电流归零确认点之前不足 {config.brake_backtrack_pulses} 个 A 相上升沿。")

    brake_delay_s = brake_end_point[0] - brake_start[0]
    if brake_delay_s < 0:
        raise ValueError("刹车终点早于刹车起点，请检查波形或参数设置。")

    brake_peak_current = current_waveform.peak_absolute_between(brake_start[0], brake_end_point[0])
    return StartupBrakeTestResult(
        startup_start_point=startup_start,
        speed_reached_point=speed_reached_point,
        startup_delay_s=speed_match.reached_time_s - startup_start[0],
        startup_peak_current=startup_peak_current,
        speed_match=speed_match,
        brake_start_point=brake_start,
        current_zero_window=current_zero_window,
        brake_end_point=brake_end_point,
        brake_delay_s=brake_delay_s,
        brake_peak_current=brake_peak_current,
        brake_mode=config.brake_mode,
    )


def _normalize_phase_degrees(phase_deg: float) -> float:
    normalized = (phase_deg + 180.0) % 360.0 - 180.0
    if normalized == -180.0 and phase_deg > 0:
        return 180.0
    return normalized


def _parse_bundle_section_header(header: str) -> dict[str, str]:
    content = header[len(BUNDLE_SECTION_PREFIX):]
    parts = [part.strip() for part in content.split(",") if part.strip()]
    if not parts:
        raise ValueError("波形包分表头缺少通道信息。")

    channel = parts[0]
    points_mode = "FILE"
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == "points_mode":
            points_mode = value.strip() or "FILE"
    return {"channel": channel, "points_mode": points_mode}
