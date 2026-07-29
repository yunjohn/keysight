from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from keysight_scope_app.services.test_profiles import TestRun


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


@dataclass(frozen=True)
class BatchRunResult:
    runs: tuple[TestRun, ...]
    requested_count: int
    cancelled: bool
    errors: tuple[str, ...] = ()


class BatchRunner:
    def run(
        self,
        sample_id: str,
        count: int,
        execute: Callable[[str, int], TestRun],
        *,
        cancellation: CancellationToken | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> BatchRunResult:
        if count <= 0:
            raise ValueError("批量运行次数必须大于 0。")
        token = cancellation or CancellationToken()
        runs: list[TestRun] = []
        errors: list[str] = []
        for index in range(count):
            if token.is_cancelled:
                break
            try:
                runs.append(execute(sample_id, index + 1))
            except Exception as exc:
                errors.append(f"第 {index + 1} 次运行失败: {exc}")
            if on_progress is not None:
                on_progress(index + 1, count)
        return BatchRunResult(tuple(runs), count, token.is_cancelled, tuple(errors))
