from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable

import pyvisa
from pyvisa.errors import VisaIOError

from keysight_scope_app.analysis.startup_brake import (
    StartupBrakeTestConfig,
    StartupBrakeTestResult,
    analyze_startup_brake_test,
)
from keysight_scope_app.utils import format_engineering_value, strip_ieee4882_block
from keysight_scope_app.analysis.waveform import (
    EdgeComparison,
    LockRecommendation,
    PeriodWindow,
    PulseWindow,
    SignalPeak,
    SpeedTargetMatch,
    WaveformData,
    WaveformPreamble,
    WaveformStats,
    ZeroStableWindow,
    _negative_pulse_width_from_stats,
    _ratio_to_percent,
    compare_waveform_edges,
)


QueryBuilder = Callable[[str], str]
StatsValueGetter = Callable[[WaveformStats], float | None]


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
class ChannelVerticalLayout:
    scale: float
    offset: float


@dataclass(frozen=True)
class EdgeTriggerSettings:
    source: str
    slope: str
    level: float
    sweep: str


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
SUPPORTED_TRIGGER_SLOPES = ("POSitive", "NEGative", "EITHer")
SUPPORTED_TRIGGER_SWEEPS = ("AUTO", "NORMal")
KNOWN_KEYSIGHT_VENDORS = ("KEYSIGHT", "AGILENT")
SOFTWARE_MEASUREMENT_POINTS = 2000
CURRENT_LIKE_MEASUREMENTS = {
    "峰峰值",
    "均方根",
    "最大值",
    "最小值",
    "平均值",
    "振幅",
    "高电平估计",
    "低电平估计",
}


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
            return self.query("*IDN?")

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

    def get_displayed_channels(self) -> list[str]:
        displayed_channels: list[str] = []
        for channel in SUPPORTED_CHANNELS:
            response = self.query(f":{channel}:DISPlay?")
            normalized = response.strip().upper()
            if normalized in {"1", "ON"}:
                displayed_channels.append(channel)
        return displayed_channels

    def set_channel_display(self, channel: str, enabled: bool) -> None:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")
        self.write(f":{channel}:DISPlay {'ON' if enabled else 'OFF'}")

    def get_channel_unit(self, channel: str) -> str:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")

        query_candidates = (
            f":{channel}:UNITs?",
            f":{channel}:PROBe:EXTernal:UNITs?",
        )
        for command in query_candidates:
            try:
                response = self.query(command)
            except Exception:
                continue
            normalized = _normalize_channel_unit(response)
            if normalized is not None:
                return normalized
        return "V"

    def get_channel_units(self, channels: list[str] | None = None) -> dict[str, str]:
        target_channels = channels or list(SUPPORTED_CHANNELS)
        return {channel: self.get_channel_unit(channel) for channel in target_channels}

    def get_channel_vertical_layout(self, channel: str) -> ChannelVerticalLayout:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")
        scale = float(self.query(f":{channel}:SCALe?"))
        offset = float(self.query(f":{channel}:OFFSet?"))
        return ChannelVerticalLayout(scale=scale, offset=offset)

    def get_channel_vertical_layouts(self, channels: list[str] | None = None) -> dict[str, ChannelVerticalLayout]:
        target_channels = channels or list(SUPPORTED_CHANNELS)
        return {channel: self.get_channel_vertical_layout(channel) for channel in target_channels}

    def get_edge_trigger_settings(self) -> EdgeTriggerSettings:
        source = _normalize_trigger_source(self.query(":TRIGger:EDGE:SOURce?"))
        slope = _normalize_trigger_slope(self.query(":TRIGger:EDGE:SLOPe?"))
        level = float(self.query(":TRIGger:EDGE:LEVel?"))
        sweep = _normalize_trigger_sweep(self.query(":TRIGger:SWEep?"))
        return EdgeTriggerSettings(
            source=source,
            slope=slope,
            level=level,
            sweep=sweep,
        )

    def apply_edge_trigger_settings(self, settings: EdgeTriggerSettings) -> None:
        source = _normalize_trigger_source(settings.source)
        slope = _normalize_trigger_slope(settings.slope)
        sweep = _normalize_trigger_sweep(settings.sweep)
        level = float(settings.level)
        self.write(":TRIGger:MODE EDGE")
        self.write(f":TRIGger:EDGE:SOURce {source}")
        self.write(f":TRIGger:EDGE:SLOPe {slope}")
        self.write(f":TRIGger:EDGE:LEVel {level}")
        self.write(f":TRIGger:SWEep {sweep}")

    def get_trigger_event_status(self) -> bool:
        response = self.query(":TER?")
        normalized = response.strip().upper()
        if normalized in {"1", "+1"}:
            return True
        if normalized in {"0", "+0"}:
            return False
        try:
            return bool(int(float(normalized)))
        except Exception as exc:
            raise ValueError(f"无法解析触发状态: {response}") from exc

    def get_max_waveform_points(
        self,
        channel: str,
        *,
        points_mode: str,
        probe_points: int = 500000,
    ) -> int:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")
        if points_mode not in SUPPORTED_WAVEFORM_POINTS_MODES:
            raise ValueError(f"不支持的波形点模式: {points_mode}")

        with self._lock:
            self._ensure_connected()
            self._instrument.write(f":WAVeform:SOURce {channel}")
            self._instrument.write(f":WAVeform:POINts:MODE {points_mode}")
            self._instrument.write(f":WAVeform:POINts {probe_points}")
            response = self._instrument.query(":WAVeform:POINts?")
        try:
            return int(float(str(response).strip()))
        except Exception:
            return probe_points

    def fetch_measurements(self, channel: str, measurement_names: list[str]) -> list[MeasurementResult]:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")

        results: list[MeasurementResult] = []
        waveform_stats: WaveformStats | None = None
        channel_unit = self.get_channel_unit(channel)
        for measurement_name in measurement_names:
            definition = MEASUREMENT_DEFINITIONS[measurement_name]
            if definition.query_builder is not None:
                raw_value = float(self.query(definition.query_builder(channel)))
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
            display_unit = _measurement_unit_for_channel(channel_unit, definition.label, definition.unit)
            results.append(
                MeasurementResult(
                    label=definition.label,
                    raw_value=raw_value,
                    unit=display_unit,
                    display_value=format_engineering_value(raw_value, display_unit),
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


def _normalize_channel_unit(response: str) -> str | None:
    normalized = response.strip().upper()
    if normalized.startswith("AMP"):
        return "A"
    if normalized.startswith("VOLT"):
        return "V"
    return None


def _measurement_unit_for_channel(channel_unit: str, measurement_label: str, default_unit: str) -> str:
    if channel_unit == "A" and measurement_label in CURRENT_LIKE_MEASUREMENTS:
        return "A"
    return default_unit


def _normalize_trigger_source(value: str) -> str:
    normalized = value.strip().upper()
    mapping = {
        "CHAN1": "CHANnel1",
        "CHANNEL1": "CHANnel1",
        "CHAN2": "CHANnel2",
        "CHANNEL2": "CHANnel2",
        "CHAN3": "CHANnel3",
        "CHANNEL3": "CHANnel3",
        "CHAN4": "CHANnel4",
        "CHANNEL4": "CHANnel4",
    }
    if normalized in mapping:
        return mapping[normalized]
    for channel in SUPPORTED_CHANNELS:
        if normalized == channel.upper():
            return channel
    raise ValueError(f"不支持的触发源: {value}")


def _normalize_trigger_slope(value: str) -> str:
    normalized = value.strip().upper()
    if normalized.startswith("POS"):
        return "POSitive"
    if normalized.startswith("NEG"):
        return "NEGative"
    if normalized.startswith("EIT") or normalized.startswith("ALT"):
        return "EITHer"
    raise ValueError(f"不支持的触发斜率: {value}")


def _normalize_trigger_sweep(value: str) -> str:
    normalized = value.strip().upper()
    if normalized.startswith("AUTO"):
        return "AUTO"
    if normalized.startswith("NORM") or normalized.startswith("TRIG"):
        return "NORMal"
    raise ValueError(f"不支持的触发扫描模式: {value}")


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
