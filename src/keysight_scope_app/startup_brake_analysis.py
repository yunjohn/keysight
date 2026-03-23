from __future__ import annotations

from dataclasses import dataclass

from keysight_scope_app.waveform_analysis import (
    SignalPeak,
    SpeedTargetMatch,
    WaveformData,
    ZeroStableWindow,
)


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
