from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from keysight_scope_app import __version__

SCHEMA_VERSION = 1


class MetricStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class MetricLimit:
    name: str
    unit: str = ""
    target: float | None = None
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class MetricResult:
    name: str
    status: MetricStatus
    value: float | None
    unit: str = ""
    target: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class TestProfile:
    __test__: ClassVar[bool] = False
    name: str
    profile_version: str
    channel_roles: dict[str, str]
    capture: dict[str, Any]
    analysis: dict[str, Any]
    metric_limits: tuple[MetricLimit, ...] = ()
    export: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=lambda: _utc_now())


@dataclass(frozen=True)
class TestRun:
    __test__: ClassVar[bool] = False
    sample_id: str
    profile_name: str
    profile_version: str
    metrics: tuple[MetricResult, ...]
    instrument_id: str = ""
    waveform_path: str | None = None
    screenshot_path: str | None = None
    run_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    app_version: str = __version__
    generated_at: str = field(default_factory=lambda: _utc_now())

    @property
    def status(self) -> MetricStatus:
        statuses = {metric.status for metric in self.metrics}
        if MetricStatus.FAIL in statuses:
            return MetricStatus.FAIL
        if MetricStatus.INCONCLUSIVE in statuses or not statuses:
            return MetricStatus.INCONCLUSIVE
        return MetricStatus.PASS


def evaluate_metric(limit: MetricLimit, value: float | None, *, reason: str = "") -> MetricResult:
    if value is None or not math.isfinite(value):
        return MetricResult(
            name=limit.name,
            status=MetricStatus.INCONCLUSIVE,
            value=value,
            unit=limit.unit,
            target=limit.target,
            minimum=limit.minimum,
            maximum=limit.maximum,
            reason=reason or "没有可用于判定的有效测量值。",
        )
    failures: list[str] = []
    if limit.minimum is not None and value < limit.minimum:
        failures.append(f"低于下限 {limit.minimum:g}{limit.unit}")
    if limit.maximum is not None and value > limit.maximum:
        failures.append(f"高于上限 {limit.maximum:g}{limit.unit}")
    return MetricResult(
        name=limit.name,
        status=MetricStatus.FAIL if failures else MetricStatus.PASS,
        value=value,
        unit=limit.unit,
        target=limit.target,
        minimum=limit.minimum,
        maximum=limit.maximum,
        reason="；".join(failures) if failures else reason or "测量值在允许范围内。",
    )


class TestProfileRepository:
    __test__: ClassVar[bool] = False

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(self, profile: TestProfile) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{_safe_name(profile.name)}.json"
        target.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def load(self, path: Path) -> TestProfile:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = int(payload.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(f"不支持的测试方案版本: {version}")
        payload["metric_limits"] = tuple(MetricLimit(**item) for item in payload.get("metric_limits", ()))
        return TestProfile(**payload)

    def list_profiles(self) -> tuple[Path, ...]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(self.directory.glob("*.json")))

    def duplicate(self, profile: TestProfile, new_name: str) -> TestProfile:
        payload = asdict(profile)
        payload.update(name=new_name, created_at=_utc_now())
        payload["metric_limits"] = tuple(MetricLimit(**item) for item in payload["metric_limits"])
        return TestProfile(**payload)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in value.strip())
    if not safe:
        raise ValueError("测试方案名称不能为空。")
    return safe
