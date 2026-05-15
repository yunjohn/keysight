from __future__ import annotations

import bisect
import csv
import math
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path


BUNDLE_MAGIC = "# KEYSIGHT_SCOPE_BUNDLE_V1"
BUNDLE_SECTION_PREFIX = "# channel="


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
    valid_transition_count: int = 0
    invalid_transition_count: int = 0
    confidence: str | None = None
    raw_transition_count: int = 0
    debounce_filtered_count: int = 0
    sequence_discarded_count: int = 0
    valid_event_points: tuple[tuple[str, str, float], ...] = ()
    invalid_event_points: tuple[tuple[str, str, float], ...] = ()


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
    pulse_count: int
    pulse_width_s: float | None
    duty_cycle: float | None
    rise_time_s: float | None
    fall_time_s: float | None


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
        pulses = _find_pulses(rising_mid_crossings, falling_mid_crossings, mid_threshold)
        estimated_frequency_hz = _estimate_frequency_from_crossings(rising_mid_crossings)
        pulse_count = len(pulses)
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
            pulse_count=pulse_count,
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

        threshold_margin = min(flat_threshold, zero_threshold * 0.1) if zero_threshold >= 0.2 else 0.0
        effective_zero_threshold = zero_threshold + threshold_margin
        point_count = min(len(self.x_values), len(self.y_values))
        start_index = bisect.bisect_left(self.x_values[:point_count], start_time)

        return _find_stable_window(
            self.x_values,
            self.y_values,
            start_index=start_index,
            target_level=0.0,
            abs_threshold=effective_zero_threshold,
            flat_threshold=flat_threshold,
            hold_time_s=hold_time_s,
            relaxed_mean_abs_limit=effective_zero_threshold,
            relaxed_std_limit=max(flat_threshold, zero_threshold * 0.5),
        )

    def find_previous_edge(
        self,
        reference_time: float,
        *,
        count: int = 1,
        edge_type: str = "rising",
        threshold_ratio: float = 0.5,
    ) -> tuple[float, float] | None:
        if not self.x_values or not self.y_values:
            return None
        if count <= 0:
            raise ValueError("回溯脉冲数必须大于 0。")
        if edge_type not in {"rising", "falling"}:
            raise ValueError(f"不支持的边沿类型: {edge_type}")

        threshold = _edge_threshold(self.y_values, edge_type=edge_type, threshold_ratio=threshold_ratio)
        crossings = [
            crossing
            for crossing in _find_crossings(self.x_values, self.y_values, threshold, edge_type)
            if crossing < reference_time
        ]
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


def _find_stable_window(
    x_values: list[float],
    y_values: list[float],
    *,
    start_index: int,
    target_level: float,
    abs_threshold: float,
    flat_threshold: float,
    hold_time_s: float,
    relaxed_mean_abs_limit: float | None = None,
    relaxed_std_limit: float | None = None,
) -> ZeroStableWindow | None:
    point_count = min(len(x_values), len(y_values))
    if start_index >= point_count:
        return None

    prefix_sum = [0.0]
    prefix_sum_sq = [0.0]
    for value in y_values[:point_count]:
        prefix_sum.append(prefix_sum[-1] + value)
        prefix_sum_sq.append(prefix_sum_sq[-1] + value * value)

    min_queue: deque[int] = deque()
    max_queue: deque[int] = deque()
    dev_queue: deque[int] = deque()
    right = start_index - 1

    def _push(index: int) -> None:
        value = y_values[index]
        while min_queue and y_values[min_queue[-1]] >= value:
            min_queue.pop()
        min_queue.append(index)
        while max_queue and y_values[max_queue[-1]] <= value:
            max_queue.pop()
        max_queue.append(index)
        deviation = abs(value - target_level)
        while dev_queue and abs(y_values[dev_queue[-1]] - target_level) <= deviation:
            dev_queue.pop()
        dev_queue.append(index)

    for left in range(start_index, point_count):
        if right < left:
            right = left
            min_queue.clear()
            max_queue.clear()
            dev_queue.clear()
            _push(right)

        while right < point_count and (x_values[right] - x_values[left]) < hold_time_s:
            right += 1
            if right >= point_count:
                break
            _push(right)
        if right >= point_count:
            break

        while min_queue and min_queue[0] < left:
            min_queue.popleft()
        while max_queue and max_queue[0] < left:
            max_queue.popleft()
        while dev_queue and dev_queue[0] < left:
            dev_queue.popleft()

        max_abs_value = abs(y_values[dev_queue[0]] - target_level)
        span_value = y_values[max_queue[0]] - y_values[min_queue[0]]
        if max_abs_value <= abs_threshold:
            if span_value <= flat_threshold:
                return ZeroStableWindow(
                    start_time_s=x_values[left],
                    confirmed_time_s=x_values[right],
                    max_abs_value=max_abs_value,
                    span_value=span_value,
                )
            if relaxed_mean_abs_limit is not None and relaxed_std_limit is not None:
                sample_count = right - left + 1
                window_sum = prefix_sum[right + 1] - prefix_sum[left]
                window_sum_sq = prefix_sum_sq[right + 1] - prefix_sum_sq[left]
                mean_value = window_sum / sample_count
                variance = max((window_sum_sq / sample_count) - (mean_value * mean_value), 0.0)
                if abs(mean_value - target_level) <= relaxed_mean_abs_limit and math.sqrt(variance) <= relaxed_std_limit:
                    return ZeroStableWindow(
                        start_time_s=x_values[left],
                        confirmed_time_s=x_values[right],
                        max_abs_value=max_abs_value,
                        span_value=span_value,
                    )
    return None


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
        actual_measure = frequency_hz
        minimum_measure = target_value * max(0.0, 1.0 - tolerance_ratio)
        return actual_measure >= minimum_measure
    elif target_mode == "period_s":
        actual_measure = period_s
        maximum_measure = target_value * (1.0 + tolerance_ratio)
        return actual_measure <= maximum_measure
    elif target_mode == "rpm":
        actual_measure = rpm
        minimum_measure = target_value * max(0.0, 1.0 - tolerance_ratio)
        return actual_measure >= minimum_measure
    else:
        raise ValueError(f"不支持的目标类型: {target_mode}")


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

    threshold_primary = (min(primary.y_values) + max(primary.y_values)) / 2 if primary.y_values else 0.0
    threshold_secondary = (min(secondary.y_values) + max(secondary.y_values)) / 2 if secondary.y_values else 0.0
    primary_crossings = _find_crossings(primary.x_values, primary.y_values, threshold_primary, edge_type)
    secondary_crossings = _find_crossings(secondary.x_values, secondary.y_values, threshold_secondary, edge_type)

    return _build_edge_comparison_from_crossings(
        primary,
        secondary,
        primary_crossings,
        secondary_crossings,
        x_hint,
        edge_type,
        frequency_hz=frequency_hz,
    )


def compare_encoder_ab_edges(
    primary: WaveformData,
    secondary: WaveformData,
    x_hint: float,
    edge_type: str,
    *,
    frequency_hz: float | None = None,
    minimum_edge_interval_s: float | None = None,
) -> EdgeComparison | None:
    validated_edges = _validated_encoder_crossings(
        primary,
        secondary,
        minimum_edge_interval_s=minimum_edge_interval_s,
    )
    if validated_edges is None:
        return None

    if frequency_hz is None or frequency_hz <= 0:
        primary_stats = primary.analyze()
        secondary_stats = secondary.analyze()
        primary_frequency_hz = primary_stats.estimated_frequency_hz
        secondary_frequency_hz = secondary_stats.estimated_frequency_hz
        if primary_frequency_hz and secondary_frequency_hz:
            frequency_hz = (primary_frequency_hz + secondary_frequency_hz) / 2.0
        else:
            frequency_hz = primary_frequency_hz or secondary_frequency_hz

    preferred = _build_edge_comparison_from_crossings(
        primary,
        secondary,
        validated_edges["primary"][edge_type],
        validated_edges["secondary"][edge_type],
        x_hint,
        edge_type,
        frequency_hz=frequency_hz,
    )
    alternate_edge_type = "falling" if edge_type == "rising" else "rising"
    alternate = _build_edge_comparison_from_crossings(
        primary,
        secondary,
        validated_edges["primary"][alternate_edge_type],
        validated_edges["secondary"][alternate_edge_type],
        x_hint,
        alternate_edge_type,
        frequency_hz=frequency_hz,
    )

    if preferred is None and alternate is None:
        return None
    if preferred is None:
        return alternate
    if alternate is None:
        return preferred

    phase_deg = _circular_mean_degrees([preferred.phase_deg, alternate.phase_deg])
    delta_t_s = statistics.mean([preferred.delta_t_s, alternate.delta_t_s])
    chosen = preferred if abs(preferred.primary_time_s - x_hint) <= abs(alternate.primary_time_s - x_hint) else alternate
    return EdgeComparison(
        edge_type=edge_type,
        primary_time_s=chosen.primary_time_s,
        secondary_time_s=chosen.secondary_time_s,
        delta_t_s=delta_t_s,
        frequency_hz=preferred.frequency_hz if preferred.frequency_hz is not None else alternate.frequency_hz,
        phase_deg=phase_deg,
        valid_transition_count=validated_edges["valid_transition_count"],
        invalid_transition_count=validated_edges["invalid_transition_count"],
        confidence=_encoder_confidence_label(
            validated_edges["valid_transition_count"],
            validated_edges["invalid_transition_count"],
        ),
        raw_transition_count=validated_edges["raw_transition_count"],
        debounce_filtered_count=validated_edges["debounce_filtered_count"],
        sequence_discarded_count=validated_edges["sequence_discarded_count"],
        valid_event_points=tuple(validated_edges["valid_event_points"]),
        invalid_event_points=tuple(validated_edges["invalid_event_points"]),
    )


def _build_edge_comparison_from_crossings(
    primary: WaveformData,
    secondary: WaveformData,
    primary_crossings: list[float],
    secondary_crossings: list[float],
    x_hint: float,
    edge_type: str,
    *,
    frequency_hz: float | None = None,
) -> EdgeComparison | None:
    if not primary_crossings or not secondary_crossings:
        return None

    averaged_delta_t_s = _average_edge_delta(primary_crossings, secondary_crossings)
    representative_pair = _representative_edge_pair(primary_crossings, secondary_crossings, x_hint)

    if representative_pair is None:
        return None

    if averaged_delta_t_s is None:
        averaged_delta_t_s = representative_pair[1] - representative_pair[0]

    if frequency_hz is None or frequency_hz <= 0:
        frequency_hz = primary.analyze().estimated_frequency_hz
    delta_t_s = averaged_delta_t_s
    phase_deg = _normalize_phase_degrees(delta_t_s * frequency_hz * 360.0) if frequency_hz and frequency_hz > 0 else None
    return EdgeComparison(
        edge_type=edge_type,
        primary_time_s=representative_pair[0],
        secondary_time_s=representative_pair[1],
        delta_t_s=delta_t_s,
        frequency_hz=frequency_hz,
        phase_deg=phase_deg,
        confidence="普通",
    )

def _validated_encoder_crossings(
    primary: WaveformData,
    secondary: WaveformData,
    *,
    minimum_edge_interval_s: float | None = None,
) -> dict[str, dict[str, list[float]] | int] | None:
    primary_events, primary_raw_count, primary_debounce_filtered = _build_encoder_channel_events(
        primary,
        channel_name="primary",
        minimum_edge_interval_s=minimum_edge_interval_s,
    )
    secondary_events, secondary_raw_count, secondary_debounce_filtered = _build_encoder_channel_events(
        secondary,
        channel_name="secondary",
        minimum_edge_interval_s=minimum_edge_interval_s,
    )
    if not primary_events or not secondary_events:
        return None

    raw_transition_count = primary_raw_count + secondary_raw_count
    debounce_filtered_count = primary_debounce_filtered + secondary_debounce_filtered
    candidate_events = sorted(primary_events + secondary_events, key=lambda event: event["time_s"])
    if len(candidate_events) < 4:
        return None
    nominal_period_s = _encoder_nominal_period(primary, secondary)
    ambiguous_spacing_s = max(
        max(primary.analyze().sample_period_s, secondary.analyze().sample_period_s) * 2.5,
        (nominal_period_s or 0.0) * 0.10,
    )
    candidate_events = _filter_ambiguous_encoder_events(candidate_events, ambiguous_spacing_s)
    if len(candidate_events) < 4:
        return None

    primary_state = _digital_state_before_time(primary, candidate_events[0]["time_s"])
    secondary_state = _digital_state_before_time(secondary, candidate_events[0]["time_s"])
    initial_state = (secondary_state << 1) | primary_state

    forward_count = 0
    reverse_count = 0
    trial_state = initial_state
    for event in candidate_events:
        step = _quadrature_step(trial_state, event["bit"])
        if step > 0:
            forward_count += 1
        elif step < 0:
            reverse_count += 1
        trial_state ^= event["bit"]

    if forward_count == 0 and reverse_count == 0:
        return None
    dominant_direction = 1 if forward_count >= reverse_count else -1

    accepted_primary: dict[str, list[float]] = {"rising": [], "falling": []}
    accepted_secondary: dict[str, list[float]] = {"rising": [], "falling": []}
    accepted_count = 0
    current_state = initial_state
    valid_event_points: list[tuple[str, str, float]] = []
    invalid_event_points: list[tuple[str, str, float]] = []
    for event in candidate_events:
        step = _quadrature_step(current_state, event["bit"])
        if step != dominant_direction:
            invalid_event_points.append((str(event["channel_name"]), str(event["edge_type"]), float(event["time_s"])))
            continue
        current_state ^= event["bit"]
        accepted_count += 1
        valid_event_points.append((str(event["channel_name"]), str(event["edge_type"]), float(event["time_s"])))
        target = accepted_primary if event["channel_name"] == "primary" else accepted_secondary
        target[event["edge_type"]].append(event["time_s"])

    if accepted_count < 4:
        return None
    for edge_type in ("rising", "falling"):
        if not accepted_primary[edge_type] or not accepted_secondary[edge_type]:
            return None

    sequence_discarded_count = max(len(candidate_events) - accepted_count, 0)
    invalid_transition_count = sequence_discarded_count
    return {
        "primary": accepted_primary,
        "secondary": accepted_secondary,
        "valid_transition_count": accepted_count,
        "invalid_transition_count": invalid_transition_count,
        "raw_transition_count": raw_transition_count,
        "debounce_filtered_count": debounce_filtered_count,
        "sequence_discarded_count": sequence_discarded_count,
        "valid_event_points": valid_event_points,
        "invalid_event_points": invalid_event_points,
    }


def _build_encoder_channel_events(
    waveform: WaveformData,
    *,
    channel_name: str,
    minimum_edge_interval_s: float | None = None,
) -> tuple[list[dict[str, object]], int, int]:
    thresholds = _encoder_schmitt_thresholds(waveform)
    if thresholds is None:
        return [], 0, 0
    rise_threshold, fall_threshold = thresholds
    rising_crossings = _find_crossings(waveform.x_values, waveform.y_values, rise_threshold, "rising")
    falling_crossings = _find_crossings(waveform.x_values, waveform.y_values, fall_threshold, "falling")
    if not rising_crossings and not falling_crossings:
        return [], 0, 0

    nominal_period_s = _estimate_nominal_period(rising_crossings)
    if nominal_period_s is None:
        nominal_period_s = _estimate_nominal_period(falling_crossings)
    auto_minimum_interval_s = max(waveform.analyze().sample_period_s * 8.0, (nominal_period_s or 0.0) * 0.15)
    minimum_interval_s = auto_minimum_interval_s if minimum_edge_interval_s is None else max(minimum_edge_interval_s, waveform.analyze().sample_period_s * 2.0)

    events = [
        {"time_s": time_s, "edge_type": "rising", "channel_name": channel_name}
        for time_s in rising_crossings
    ] + [
        {"time_s": time_s, "edge_type": "falling", "channel_name": channel_name}
        for time_s in falling_crossings
    ]
    raw_event_count = len(events)
    events.sort(key=lambda event: float(event["time_s"]))

    filtered_events: list[dict[str, object]] = []
    last_time_s: float | None = None
    for event in events:
        time_s = float(event["time_s"])
        if last_time_s is not None and (time_s - last_time_s) < minimum_interval_s:
            continue
        filtered_events.append(event)
        last_time_s = time_s

    bit = 0b01 if channel_name == "primary" else 0b10
    for event in filtered_events:
        event["bit"] = bit
    debounce_filtered_count = max(raw_event_count - len(filtered_events), 0)
    return filtered_events, raw_event_count, debounce_filtered_count


def _encoder_confidence_label(valid_transition_count: int, invalid_transition_count: int) -> str:
    total = valid_transition_count + invalid_transition_count
    if total <= 0:
        return "低"
    valid_ratio = valid_transition_count / total
    if valid_transition_count >= 10 and valid_ratio >= 0.75:
        return "高"
    if valid_transition_count >= 6 and valid_ratio >= 0.50:
        return "中"
    return "低"


def _encoder_schmitt_thresholds(waveform: WaveformData) -> tuple[float, float] | None:
    stats = waveform.analyze()
    amplitude = stats.logic_high_v - stats.logic_low_v
    if amplitude <= 0:
        return None
    rise_threshold = stats.logic_low_v + amplitude * 0.65
    fall_threshold = stats.logic_low_v + amplitude * 0.35
    return rise_threshold, fall_threshold


def _digital_state_before_time(waveform: WaveformData, time_s: float) -> int:
    thresholds = _encoder_schmitt_thresholds(waveform)
    if thresholds is None or not waveform.x_values or not waveform.y_values:
        return 0
    rise_threshold, fall_threshold = thresholds
    midpoint = (rise_threshold + fall_threshold) / 2.0
    state = 1 if waveform.y_values[0] >= midpoint else 0
    limit = bisect.bisect_right(waveform.x_values, time_s)
    for value in waveform.y_values[1:limit]:
        if state == 0 and value >= rise_threshold:
            state = 1
        elif state == 1 and value <= fall_threshold:
            state = 0
    return state


def _quadrature_step(current_state: int, bit: int) -> int:
    next_state = current_state ^ bit
    gray_cycle = (0b00, 0b01, 0b11, 0b10)
    try:
        current_index = gray_cycle.index(current_state)
        next_index = gray_cycle.index(next_state)
    except ValueError:
        return 0
    if next_index == (current_index + 1) % len(gray_cycle):
        return 1
    if next_index == (current_index - 1) % len(gray_cycle):
        return -1
    return 0


def _estimate_nominal_period(crossings: list[float]) -> float | None:
    if len(crossings) < 2:
        return None
    periods = [
        crossings[index] - crossings[index - 1]
        for index in range(1, len(crossings))
        if crossings[index] > crossings[index - 1]
    ]
    if not periods:
        return None
    return statistics.median(periods)


def _encoder_nominal_period(primary: WaveformData, secondary: WaveformData) -> float | None:
    primary_stats = primary.analyze()
    secondary_stats = secondary.analyze()
    candidate_periods = []
    if primary_stats.estimated_frequency_hz and primary_stats.estimated_frequency_hz > 0:
        candidate_periods.append(1.0 / primary_stats.estimated_frequency_hz)
    if secondary_stats.estimated_frequency_hz and secondary_stats.estimated_frequency_hz > 0:
        candidate_periods.append(1.0 / secondary_stats.estimated_frequency_hz)
    if not candidate_periods:
        return None
    return statistics.median(candidate_periods)


def _filter_ambiguous_encoder_events(
    events: list[dict[str, object]],
    minimum_spacing_s: float,
) -> list[dict[str, object]]:
    if minimum_spacing_s <= 0 or len(events) < 2:
        return events

    filtered_events: list[dict[str, object]] = []
    index = 0
    while index < len(events):
        current = events[index]
        if index + 1 < len(events):
            following = events[index + 1]
            current_time = float(current["time_s"])
            following_time = float(following["time_s"])
            if (
                str(current["channel_name"]) != str(following["channel_name"])
                and (following_time - current_time) < minimum_spacing_s
            ):
                index += 2
                continue
        filtered_events.append(current)
        index += 1
    return filtered_events


def _average_edge_delta(primary_crossings: list[float], secondary_crossings: list[float]) -> float | None:
    aligned_pairs = _aligned_crossing_pairs(primary_crossings, secondary_crossings)
    if not aligned_pairs:
        return None
    deltas = [secondary_time - primary_time for primary_time, secondary_time in aligned_pairs]
    return statistics.median(deltas)


def _representative_edge_pair(
    primary_crossings: list[float],
    secondary_crossings: list[float],
    x_hint: float,
) -> tuple[float, float] | None:
    aligned_pairs = _aligned_crossing_pairs(primary_crossings, secondary_crossings)
    if not aligned_pairs:
        return None
    return min(aligned_pairs, key=lambda pair: abs(((pair[0] + pair[1]) / 2.0) - x_hint))


def _aligned_crossing_pairs(primary_crossings: list[float], secondary_crossings: list[float]) -> list[tuple[float, float]]:
    if not primary_crossings or not secondary_crossings:
        return []

    best_pairs: list[tuple[float, float]] = []
    best_score: float | None = None
    max_offset = min(3, len(primary_crossings) - 1, len(secondary_crossings) - 1)
    for offset in range(-max_offset, max_offset + 1):
        if offset >= 0:
            primary_slice = primary_crossings[: len(secondary_crossings) - offset]
            secondary_slice = secondary_crossings[offset : offset + len(primary_slice)]
        else:
            secondary_slice = secondary_crossings[: len(primary_crossings) + offset]
            primary_slice = primary_crossings[-offset : -offset + len(secondary_slice)]
        if not primary_slice or not secondary_slice:
            continue
        pair_count = min(len(primary_slice), len(secondary_slice))
        pairs = list(zip(primary_slice[:pair_count], secondary_slice[:pair_count]))
        if pair_count < 2:
            continue
        deltas = [secondary_time - primary_time for primary_time, secondary_time in pairs]
        score = abs(statistics.median(deltas))
        if best_score is None or score < best_score:
            best_score = score
            best_pairs = pairs

    if best_pairs:
        return best_pairs

    pair_count = min(len(primary_crossings), len(secondary_crossings))
    return list(zip(primary_crossings[:pair_count], secondary_crossings[:pair_count]))


def _normalize_phase_degrees(phase_deg: float) -> float:
    normalized = (phase_deg + 180.0) % 360.0 - 180.0
    if normalized == -180.0 and phase_deg > 0:
        return 180.0
    return normalized


def _circular_mean_degrees(values: list[float | None]) -> float | None:
    valid_values = [value for value in values if value is not None]
    if not valid_values:
        return None
    sin_sum = sum(math.sin(math.radians(value)) for value in valid_values)
    cos_sum = sum(math.cos(math.radians(value)) for value in valid_values)
    if abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12:
        return None
    return _normalize_phase_degrees(math.degrees(math.atan2(sin_sum, cos_sum)))


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
