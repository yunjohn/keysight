from __future__ import annotations

from dataclasses import dataclass

from keysight_scope_app.analysis.waveform import (
    SignalPeak,
    SpeedTargetMatch,
    WaveformData,
    ZeroStableWindow,
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
    control_threshold_ratio: float = 0.1
    startup_min_voltage_step: float = 1.0
    startup_hold_s: float = 0.001
    zero_current_threshold_a: float = 0.5
    zero_current_flat_threshold_a: float = 0.03
    zero_current_hold_s: float = 0.002
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

        brake_start = control_waveform.find_previous_edge(
            speed_zero_window.start_time_s,
            count=1,
            edge_type="falling",
            threshold_ratio=config.control_threshold_ratio,
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
) -> tuple[float, float] | None:
    if not waveform.x_values or not waveform.y_values:
        return None
    if min_voltage_step < 0:
        raise ValueError("启动最小跳变不能为负数。")
    if hold_time_s < 0:
        raise ValueError("启动保持时间不能为负数。")

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
                return time_value, required_level
            previous_time = waveform.x_values[index - 1]
            previous_value = waveform.y_values[index - 1]
            delta_value = signal_value - previous_value
            if delta_value == 0:
                return time_value, required_level
            ratio = (required_level - previous_value) / delta_value
            crossing_time = previous_time + ratio * (time_value - previous_time)
            return crossing_time, required_level
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
