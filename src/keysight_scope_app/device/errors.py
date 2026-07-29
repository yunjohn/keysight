from __future__ import annotations


class ScopeError(RuntimeError):
    """Base error for user-actionable oscilloscope failures."""


class ScopeConnectionError(ScopeError):
    pass


class ScopeSessionError(ScopeError):
    pass


class ScopeCommandUnsupportedError(ScopeError):
    pass


class ScopeTimeoutError(ScopeError):
    pass


class ScopeResponseError(ScopeError):
    pass


class WaveformIntegrityError(ScopeResponseError):
    pass
