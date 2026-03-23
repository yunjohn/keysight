from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable

import pyvisa
from pyvisa.errors import VisaIOError

from keysight_scope_app.startup_brake_analysis import (
    StartupBrakeTestConfig,
    StartupBrakeTestResult,
    analyze_startup_brake_test,
)
from keysight_scope_app.utils import format_engineering_value, strip_ieee4882_block
from keysight_scope_app.waveform_analysis import (
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

    def fetch_measurements(self, channel: str, measurement_names: list[str]) -> list[MeasurementResult]:
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"不支持的通道: {channel}")

        results: list[MeasurementResult] = []
        waveform_stats: WaveformStats | None = None
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
