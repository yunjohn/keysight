from __future__ import annotations

from dataclasses import dataclass

from keysight_scope_app.analysis.waveform import (
    SignalPeak,
    SpeedTargetMatch,
    WaveformData,
    ZeroStableWindow,
    _edge_threshold,
    _find_crossings,
    _find_stable_window,
)


@dataclass(frozen=True)
class StartupBrakeTestConfig:
    control_channel: str
    speed_channel: str
    current_channel: str
    encoder_a_channel: str | None = None
    test_scope_mode: str = "full"
    speed_target_mode: str = "frequency_hz"
    speed_target_value: float = 0.0
    speed_tolerance_ratio: float = 0.05
    speed_consecutive_periods: int = 3
    pulses_per_revolution: int = 1
    control_threshold_ratio: float = 0.02
    startup_min_voltage_step: float = 1.0
    startup_hold_s: float = 0.001
    startup_min_rise_s: float = 0.0
    startup_max_rise_s: float = 0.0
    zero_current_threshold_a: float = 0.5
    zero_current_flat_threshold_a: float = 0.03
    zero_current_hold_s: float = 0.002
    brake_low_hold_s: float = 0.002
    brake_min_fall_s: float = 0.0
    brake_max_fall_s: float = 0.0
    brake_mode: str = "current_zero"
    brake_backtrack_pulses: int = 8
    brake_backtrack_min_step: float = 0.0
    brake_backtrack_min_interval_s: float = 0.0
    startup_delay_limit_s: float | None = None
    brake_delay_limit_s: float | None = None
    startup_peak_limit_a: float | None = None
    brake_peak_limit_a: float | None = None


@dataclass(frozen=True)
class StartupBrakeTestResult:
    startup_start_point: tuple[float, float] | None
    speed_reached_point: tuple[float, float] | None
    startup_delay_s: float | None
    startup_peak_current: SignalPeak | None
    speed_match: SpeedTargetMatch | None
    brake_start_point: tuple[float, float] | None
    current_zero_window: ZeroStableWindow | None
    brake_end_point: tuple[float, float] | None
    brake_delay_s: float | None
    brake_peak_current: SignalPeak | None
    brake_mode: str
    test_scope_mode: str


def analyze_startup_brake_test(
    waveforms: list[WaveformData],
    config: StartupBrakeTestConfig,
) -> StartupBrakeTestResult:
    requires_startup = config.test_scope_mode in {"full", "startup_only"}
    requires_brake = config.test_scope_mode in {"full", "brake_only"}
    encoder_channel = config.encoder_a_channel if config.brake_mode == "encoder_backtrack" and requires_brake else None
    waveform_map = {waveform.channel: waveform for waveform in waveforms}
    missing_channels = [
        channel
        for channel in (
            config.control_channel,
            config.speed_channel if (requires_startup or requires_brake) else None,
            config.current_channel,
            encoder_channel,
        )
        if channel and channel not in waveform_map
    ]
    if missing_channels:
        raise ValueError(f"缺少测试所需通道波形: {', '.join(missing_channels)}")

    if requires_startup and config.speed_target_value <= 0:
        raise ValueError("目标转速/频率/周期必须大于 0。")
    if config.test_scope_mode not in {"full", "startup_only", "brake_only"}:
        raise ValueError(f"不支持的测试模式: {config.test_scope_mode}")
    if config.brake_mode not in {"current_zero", "encoder_backtrack"}:
        raise ValueError(f"不支持的刹车模式: {config.brake_mode}")
    if config.brake_backtrack_min_step < 0:
        raise ValueError("回溯脉冲最小跳变不能为负数。")
    if config.brake_backtrack_min_interval_s < 0:
        raise ValueError("回溯脉冲最小间隔不能为负数。")

    control_waveform = waveform_map[config.control_channel]
    current_waveform = waveform_map[config.current_channel]
    speed_waveform = waveform_map.get(config.speed_channel)

    startup_start: tuple[float, float] | None = None
    speed_reached_point: tuple[float, float] | None = None
    startup_delay_s: float | None = None
    startup_peak_current: SignalPeak | None = None
    speed_match: SpeedTargetMatch | None = None
    if requires_startup:
        startup_start = _find_startup_edge(
            control_waveform,
            threshold_ratio=config.control_threshold_ratio,
            min_voltage_step=config.startup_min_voltage_step,
            hold_time_s=config.startup_hold_s,
            min_rise_s=config.startup_min_rise_s,
            max_rise_s=config.startup_max_rise_s,
        )
        if startup_start is None:
            raise ValueError("未检测到满足跳变与保持条件的控制器启动上升沿。")

        if speed_waveform is None:
            raise ValueError("缺少启动测试所需转速通道波形。")
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
        startup_delay_s = speed_match.reached_time_s - startup_start[0]

    brake_start: tuple[float, float] | None = None
    current_zero_window: ZeroStableWindow | None = None
    brake_end_point: tuple[float, float] | None = None
    brake_delay_s: float | None = None
    brake_peak_current: SignalPeak | None = None
    if requires_brake:
        if speed_waveform is None:
            raise ValueError("缺少刹车测试所需转速通道波形。")

        speed_zero_window = _find_speed_zero_window(
            speed_waveform,
            start_time=startup_start[0] if startup_start is not None else speed_waveform.x_values[0],
        )
        if speed_zero_window is None:
            raise ValueError("未检测到转速归零稳定区间。")

        slowdown_onset_time = _find_speed_slowdown_onset(
            speed_waveform,
            start_time=startup_start[0] if startup_start is not None else speed_waveform.x_values[0],
            tolerance_ratio=max(config.speed_tolerance_ratio, 0.01),
            consecutive_periods=max(config.speed_consecutive_periods, 2),
        )

        primary_reference_time = slowdown_onset_time if slowdown_onset_time is not None else speed_zero_window.start_time_s
        brake_start = _find_brake_start_edge(
            control_waveform,
            reference_time=primary_reference_time,
            threshold_ratio=config.control_threshold_ratio,
            low_hold_s=config.brake_low_hold_s,
            min_fall_s=config.brake_min_fall_s,
            max_fall_s=config.brake_max_fall_s,
        )
        if brake_start is None and primary_reference_time != speed_zero_window.start_time_s:
            brake_start = _find_brake_start_edge(
                control_waveform,
                reference_time=speed_zero_window.start_time_s,
                threshold_ratio=config.control_threshold_ratio,
                low_hold_s=config.brake_low_hold_s,
                min_fall_s=config.brake_min_fall_s,
                max_fall_s=config.brake_max_fall_s,
            )
        if brake_start is None:
            raise ValueError("未检测到转速归零前的控制器下降沿。")

        current_zero_window = current_waveform.find_zero_stable_window(
            start_time=brake_start[0],
            zero_threshold=config.zero_current_threshold_a,
            flat_threshold=config.zero_current_flat_threshold_a,
            hold_time_s=config.zero_current_hold_s,
        )
        if current_zero_window is None:
            raise ValueError("未检测到满足阈值条件的零电流稳定区间。")

        if config.brake_mode == "current_zero":
            brake_end_point = (current_zero_window.start_time_s, 0.0)
        else:
            if not encoder_channel:
                raise ValueError("A 相回溯模式需要选择编码器 A 相通道。")
            encoder_waveform = waveform_map[encoder_channel]
            brake_end_point = _find_previous_filtered_edge(
                encoder_waveform,
                reference_time=current_zero_window.confirmed_time_s,
                start_time=brake_start[0],
                count=config.brake_backtrack_pulses,
                edge_type="rising",
                min_step=config.brake_backtrack_min_step,
                min_interval_s=config.brake_backtrack_min_interval_s,
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
        startup_delay_s=startup_delay_s,
        startup_peak_current=startup_peak_current,
        speed_match=speed_match,
        brake_start_point=brake_start,
        current_zero_window=current_zero_window,
        brake_end_point=brake_end_point,
        brake_delay_s=brake_delay_s,
        brake_peak_current=brake_peak_current,
        brake_mode=config.brake_mode,
        test_scope_mode=config.test_scope_mode,
    )


def _find_startup_edge(
    waveform: WaveformData,
    *,
    threshold_ratio: float,
    min_voltage_step: float,
    hold_time_s: float,
    min_rise_s: float,
    max_rise_s: float,
) -> tuple[float, float] | None:
    if not waveform.x_values or not waveform.y_values:
        return None
    if min_voltage_step < 0:
        raise ValueError("启动最小跳变不能为负数。")
    if hold_time_s < 0:
        raise ValueError("启动保持时间不能为负数。")
    if min_rise_s < 0 or max_rise_s < 0:
        raise ValueError("启动上升时间门限不能为负数。")
    if max_rise_s and min_rise_s > max_rise_s:
        raise ValueError("启动最小上升时间不能大于最大上升时间。")

    threshold_edge = waveform.find_first_edge("rising", threshold_ratio=threshold_ratio)
    if threshold_edge is None:
        return None

    low_level = min(waveform.y_values)
    point_count = min(len(waveform.x_values), len(waveform.y_values))
    threshold = threshold_edge[1]
    required_level = max(threshold, low_level + min_voltage_step)

    for index in range(point_count):
        time_value = waveform.x_values[index]
        signal_value = waveform.y_values[index]
        if signal_value < required_level:
            continue

        hold_end_time = time_value + hold_time_s
        valid = True
        probe_index = index
        while probe_index < point_count and waveform.x_values[probe_index] <= hold_end_time:
            if waveform.y_values[probe_index] < required_level:
                valid = False
                break
            probe_index += 1

        if valid and (hold_time_s == 0 or (probe_index < point_count or waveform.x_values[-1] >= hold_end_time)):
            if index == 0:
                crossing_time = time_value
            else:
                previous_time = waveform.x_values[index - 1]
                previous_value = waveform.y_values[index - 1]
                delta_value = signal_value - previous_value
                if delta_value == 0:
                    crossing_time = time_value
                else:
                    ratio = (required_level - previous_value) / delta_value
                    crossing_time = previous_time + ratio * (time_value - previous_time)
            rise_duration_s = _transition_duration_around_time(waveform, edge_type="rising", anchor_time=crossing_time)
            if _duration_within_limits(rise_duration_s, minimum_s=min_rise_s, maximum_s=max_rise_s):
                return crossing_time, required_level
    return None


def _find_brake_start_edge(
    waveform: WaveformData,
    *,
    reference_time: float,
    threshold_ratio: float,
    low_hold_s: float,
    min_fall_s: float,
    max_fall_s: float,
) -> tuple[float, float] | None:
    if not waveform.x_values or not waveform.y_values:
        return None
    if low_hold_s < 0:
        raise ValueError("刹车低电平保持时间不能为负数。")

    preview_threshold = _logic_edge_threshold(waveform, edge_type="falling", threshold_ratio=threshold_ratio)
    stats = waveform.analyze()
    amplitude = max(stats.logic_high_v - stats.logic_low_v, 0.0)
    confirm_threshold = stats.logic_low_v + amplitude * 0.2
    confirm_rebound_threshold = confirm_threshold + amplitude * 0.05
    confirm_hold_s = max(low_hold_s, waveform.preamble.x_increment * 2.0, 0.0001)
    max_confirm_delay_s = max(waveform.preamble.x_increment * 80.0, 0.03)

    falling_edges = [
        crossing
        for crossing in _find_crossings(waveform.x_values, waveform.y_values, preview_threshold, "falling")
        if crossing <= reference_time
    ]
    if not falling_edges or amplitude <= 0:
        return None
    for crossing in reversed(falling_edges):
        fall_duration_s = _transition_duration_around_time(waveform, edge_type="falling", anchor_time=crossing)
        if not _duration_within_limits(fall_duration_s, minimum_s=min_fall_s, maximum_s=max_fall_s):
            continue
        if _falling_edge_reaches_low_region(
            waveform,
            crossing_time=crossing,
            confirm_threshold=confirm_threshold,
            confirm_rebound_threshold=confirm_rebound_threshold,
            confirm_hold_s=confirm_hold_s,
            max_confirm_delay_s=max_confirm_delay_s,
        ):
            return crossing, preview_threshold
    return None


def _logic_edge_threshold(
    waveform: WaveformData,
    *,
    edge_type: str,
    threshold_ratio: float,
) -> float:
    stats = waveform.analyze()
    logic_low = stats.logic_low_v
    logic_high = stats.logic_high_v
    amplitude = logic_high - logic_low
    if edge_type == "rising":
        return logic_low + amplitude * threshold_ratio
    if edge_type == "falling":
        return logic_high - amplitude * threshold_ratio
    raise ValueError(f"不支持的边沿类型: {edge_type}")


def _duration_within_limits(duration_s: float | None, *, minimum_s: float, maximum_s: float) -> bool:
    if minimum_s <= 0 and maximum_s <= 0:
        return True
    if duration_s is None:
        return False
    if minimum_s > 0 and duration_s < minimum_s:
        return False
    if maximum_s > 0 and duration_s > maximum_s:
        return False
    return True


def _transition_duration_around_time(
    waveform: WaveformData,
    *,
    edge_type: str,
    anchor_time: float,
) -> float | None:
    stats = waveform.analyze()
    amplitude = max(stats.logic_high_v - stats.logic_low_v, 0.0)
    if amplitude <= 0:
        return None

    low_threshold = stats.logic_low_v + amplitude * 0.1
    high_threshold = stats.logic_low_v + amplitude * 0.9
    if edge_type == "rising":
        start_crossings = _find_crossings(waveform.x_values, waveform.y_values, low_threshold, "rising")
        end_crossings = _find_crossings(waveform.x_values, waveform.y_values, high_threshold, "rising")
    elif edge_type == "falling":
        start_crossings = _find_crossings(waveform.x_values, waveform.y_values, high_threshold, "falling")
        end_crossings = _find_crossings(waveform.x_values, waveform.y_values, low_threshold, "falling")
    else:
        raise ValueError(f"不支持的边沿类型: {edge_type}")

    end_index = 0
    for start_time in start_crossings:
        while end_index < len(end_crossings) and end_crossings[end_index] < start_time:
            end_index += 1
        if end_index >= len(end_crossings):
            break
        end_time = end_crossings[end_index]
        if start_time <= anchor_time <= end_time:
            return end_time - start_time
    return None


def _falling_edge_reaches_low_region(
    waveform: WaveformData,
    *,
    crossing_time: float,
    confirm_threshold: float,
    confirm_rebound_threshold: float,
    confirm_hold_s: float,
    max_confirm_delay_s: float,
) -> bool:
    point_count = min(len(waveform.x_values), len(waveform.y_values))
    start_index = 0
    while start_index < point_count and waveform.x_values[start_index] < crossing_time:
        start_index += 1

    low_entry_index: int | None = None
    for index in range(start_index, point_count):
        time_value = waveform.x_values[index]
        if time_value - crossing_time > max_confirm_delay_s:
            return False
        if waveform.y_values[index] <= confirm_threshold:
            low_entry_index = index
            break

    if low_entry_index is None:
        return False

    hold_until = waveform.x_values[low_entry_index] + confirm_hold_s
    probe_index = low_entry_index
    while probe_index < point_count and waveform.x_values[probe_index] <= hold_until:
        if waveform.y_values[probe_index] > confirm_rebound_threshold:
            return False
        probe_index += 1
    return probe_index > low_entry_index


def _find_speed_slowdown_onset(
    waveform: WaveformData,
    *,
    start_time: float,
    tolerance_ratio: float,
    consecutive_periods: int,
) -> float | None:
    if not waveform.x_values or not waveform.y_values:
        return None

    threshold = _edge_threshold(waveform.y_values, edge_type="rising", threshold_ratio=0.5)
    crossings = [crossing for crossing in _find_crossings(waveform.x_values, waveform.y_values, threshold, "rising") if crossing >= start_time]
    if len(crossings) < consecutive_periods + 3:
        return None

    periods = [
        (crossings[index], crossings[index + 1] - crossings[index])
        for index in range(len(crossings) - 1)
    ]
    for index in range(3, len(periods) - consecutive_periods + 1):
        baseline_samples = [period for _, period in periods[max(0, index - 5):index]]
        if len(baseline_samples) < 3:
            continue
        sorted_baseline = sorted(baseline_samples)
        baseline = sorted_baseline[len(sorted_baseline) // 2]
        if baseline <= 0:
            continue
        slowdown_window = periods[index:index + consecutive_periods]
        if all(period >= baseline * (1.0 + tolerance_ratio) for _, period in slowdown_window):
            return slowdown_window[0][0]
    return None


def _find_speed_zero_window(
    waveform: WaveformData,
    *,
    start_time: float,
) -> ZeroStableWindow | None:
    if not waveform.x_values or not waveform.y_values:
        return None

    stats = waveform.analyze()
    low_level = stats.logic_low_v
    amplitude = max(stats.logic_high_v - stats.logic_low_v, 0.0)
    zero_threshold = max(amplitude * 0.15, 0.05)
    flat_threshold = max(amplitude * 0.1, 0.03)

    last_falling_edge = waveform.find_previous_edge(
        waveform.x_values[-1] + waveform.preamble.x_increment,
        count=1,
        edge_type="falling",
    )
    search_start_time = start_time
    if last_falling_edge is not None and last_falling_edge[0] >= start_time:
        search_start_time = last_falling_edge[0]

    point_count = min(len(waveform.x_values), len(waveform.y_values))
    start_index = 0
    while start_index < point_count and waveform.x_values[start_index] < search_start_time:
        start_index += 1

    return _find_stable_window(
        waveform.x_values,
        waveform.y_values,
        start_index=start_index,
        target_level=low_level,
        abs_threshold=zero_threshold,
        flat_threshold=flat_threshold,
        hold_time_s=max(waveform.preamble.x_increment * 4, 0.002),
    )
def _find_previous_filtered_edge(
    waveform: WaveformData,
    *,
    reference_time: float,
    start_time: float,
    count: int,
    edge_type: str,
    min_step: float,
    min_interval_s: float,
) -> tuple[float, float] | None:
    if not waveform.x_values or not waveform.y_values:
        return None
    if count <= 0:
        raise ValueError("回溯脉冲数必须大于 0。")
    if edge_type not in {"rising", "falling"}:
        raise ValueError(f"不支持的边沿类型: {edge_type}")

    threshold = (max(waveform.y_values) + min(waveform.y_values)) / 2.0
    accepted_crossings: list[float] = []
    previous_kept_crossing: float | None = None
    point_count = min(len(waveform.x_values), len(waveform.y_values))

    for index in range(1, point_count):
        previous_value = waveform.y_values[index - 1]
        current_value = waveform.y_values[index]
        crossing_time = _crossing_time(
            waveform.x_values[index - 1],
            waveform.x_values[index],
            previous_value,
            current_value,
            threshold,
            edge_type,
        )
        if crossing_time is None:
            continue
        if crossing_time < start_time or crossing_time >= reference_time:
            continue
        if abs(current_value - previous_value) < min_step:
            continue
        if previous_kept_crossing is not None and (crossing_time - previous_kept_crossing) < min_interval_s:
            continue
        accepted_crossings.append(crossing_time)
        previous_kept_crossing = crossing_time

    if len(accepted_crossings) < count:
        return None
    return accepted_crossings[-count], threshold


def _crossing_time(
    previous_time: float,
    current_time: float,
    previous_value: float,
    current_value: float,
    threshold: float,
    edge_type: str,
) -> float | None:
    is_crossing = (
        edge_type == "rising" and previous_value < threshold <= current_value
    ) or (
        edge_type == "falling" and previous_value > threshold >= current_value
    )
    if not is_crossing:
        return None
    delta_value = current_value - previous_value
    if delta_value == 0:
        return current_time
    ratio = (threshold - previous_value) / delta_value
    return previous_time + ratio * (current_time - previous_time)
