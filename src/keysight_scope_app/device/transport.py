from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pyvisa.errors import InvalidSession, VisaIOError

from keysight_scope_app.device.errors import (
    ScopeConnectionError,
    ScopeResponseError,
    ScopeSessionError,
    ScopeTimeoutError,
)

LOGGER = logging.getLogger(__name__)
VISA_TIMEOUT = -1073807339


@runtime_checkable
class ScopeTransport(Protocol):
    @property
    def is_open(self) -> bool: ...

    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def query_ascii(self, command: str) -> list[float]: ...

    def query_binary(self, command: str) -> bytes: ...

    def close(self) -> None: ...


def redact_resource_name(resource_name: str) -> str:
    return re.sub(
        r"(TCPIP\d*::)([^:]+)",
        lambda match: f"{match.group(1)}<redacted>",
        resource_name,
        flags=re.IGNORECASE,
    )


class VisaScopeTransport:
    def __init__(self, resource_manager: Any, instrument: Any, resource_name: str) -> None:
        self._resource_manager = resource_manager
        self._instrument = instrument
        self.resource_name = resource_name
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, command: str) -> None:
        self._invoke("write", command, lambda: self._instrument.write(command))

    def query(self, command: str) -> str:
        return str(self._invoke("query", command, lambda: self._instrument.query(command))).strip()

    def query_ascii(self, command: str) -> list[float]:
        result = self._invoke("query_ascii", command, lambda: self._instrument.query_ascii_values(command))
        try:
            return [float(value) for value in result]
        except (TypeError, ValueError) as exc:
            raise ScopeResponseError(f"无法解析示波器数值响应: {command}") from exc

    def query_binary(self, command: str) -> bytes:
        result = self._invoke(
            "query_binary",
            command,
            lambda: self._instrument.query_binary_values(
                command,
                datatype="B",
                container=bytearray,
                header_fmt="ieee",
                expect_termination=False,
            ),
        )
        return bytes(result)

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        errors: list[Exception] = []
        for close in (self._instrument.close, self._resource_manager.close):
            try:
                close()
            except (InvalidSession, VisaIOError) as exc:
                errors.append(exc)
        if errors:
            LOGGER.debug("VISA close completed with %d ignored session errors", len(errors))

    def _invoke(self, operation: str, command: str, callback: Callable[[], Any]) -> Any:
        if not self._open:
            raise ScopeSessionError("示波器会话已关闭，请重新连接设备。")
        started = time.perf_counter()
        try:
            return callback()
        except InvalidSession as exc:
            self._open = False
            raise ScopeSessionError("示波器会话已失效，请重新连接设备。") from exc
        except VisaIOError as exc:
            if getattr(exc, "error_code", None) == VISA_TIMEOUT:
                raise ScopeTimeoutError(f"示波器命令超时: {command}") from exc
            raise ScopeConnectionError(f"示波器通信失败: {command}") from exc
        finally:
            LOGGER.debug(
                "scope_command operation=%s command=%s duration_ms=%.1f resource=%s",
                operation,
                command.split(maxsplit=1)[0],
                (time.perf_counter() - started) * 1000,
                redact_resource_name(self.resource_name),
            )


class ScriptedScopeTransport:
    """Deterministic transport for tests and offline demonstrations."""

    def __init__(
        self,
        *,
        queries: dict[str, str] | None = None,
        ascii_queries: dict[str, list[float]] | None = None,
        binary_queries: dict[str, bytes] | None = None,
    ) -> None:
        self.queries = dict(queries or {})
        self.ascii_queries = {key: list(value) for key, value in (ascii_queries or {}).items()}
        self.binary_queries = dict(binary_queries or {})
        self.commands: list[tuple[str, str]] = []
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, command: str) -> None:
        self._check_open()
        self.commands.append(("write", command))

    def query(self, command: str) -> str:
        self._check_open()
        self.commands.append(("query", command))
        if command not in self.queries:
            raise ScopeResponseError(f"模拟设备没有配置响应: {command}")
        return self.queries[command]

    def query_ascii(self, command: str) -> list[float]:
        self._check_open()
        self.commands.append(("query_ascii", command))
        if command not in self.ascii_queries:
            raise ScopeResponseError(f"模拟设备没有配置数值响应: {command}")
        return list(self.ascii_queries[command])

    def query_binary(self, command: str) -> bytes:
        self._check_open()
        self.commands.append(("query_binary", command))
        if command not in self.binary_queries:
            raise ScopeResponseError(f"模拟设备没有配置二进制响应: {command}")
        return bytes(self.binary_queries[command])

    def close(self) -> None:
        self._open = False

    def _check_open(self) -> None:
        if not self._open:
            raise ScopeSessionError("模拟示波器会话已关闭。")
