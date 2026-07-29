from pathlib import Path

import pytest

from keysight_scope_app.device.errors import ScopeResponseError, ScopeSessionError, WaveformIntegrityError
from keysight_scope_app.device.instrument import KeysightOscilloscope
from keysight_scope_app.device.models import CaptureRequest
from keysight_scope_app.device.transport import ScriptedScopeTransport, redact_resource_name


def _transport(payload: bytes = bytes([1, 2, 3])) -> ScriptedScopeTransport:
    return ScriptedScopeTransport(
        queries={
            "*IDN?": "KEYSIGHT,DSOX,MY123,1.0",
            ":CHANnel1:UNITs?": "V",
            ":CHANnel1:SCALe?": "1",
            ":CHANnel1:OFFSet?": "0",
        },
        ascii_queries={":WAVeform:PREamble?": [0, 0, 3, 1, 0.001, 0, 0, 1, 0, 0]},
        binary_queries={":WAVeform:DATA?": payload, ":DISPlay:DATA? PNG, COLor": b"png"},
    )


def test_scripted_transport_supports_offline_capture() -> None:
    scope = KeysightOscilloscope("SIM::SCOPE", transport=_transport())
    assert scope.connect().startswith("KEYSIGHT")

    result = scope.capture(CaptureRequest(("CHANnel1",), points=3))

    assert result.waveforms[0].y_values == [1.0, 2.0, 3.0]
    assert result.channel_units == {"CHANnel1": "V"}
    assert result.elapsed_s >= 0


def test_capture_screenshot_uses_transport(tmp_path: Path) -> None:
    scope = KeysightOscilloscope("SIM::SCOPE", transport=_transport())
    target = scope.capture_screenshot(tmp_path / "scope.png")
    assert target.read_bytes() == b"png"


def test_waveform_integrity_rejects_empty_payload() -> None:
    scope = KeysightOscilloscope("SIM::SCOPE", transport=_transport(b""))
    with pytest.raises(WaveformIntegrityError, match="空波形"):
        scope.fetch_waveform("CHANnel1", points=3)


def test_scripted_transport_missing_response_is_explicit() -> None:
    transport = ScriptedScopeTransport()
    with pytest.raises(ScopeResponseError, match="没有配置响应"):
        transport.query("*IDN?")


def test_scripted_transport_close_is_idempotent() -> None:
    transport = ScriptedScopeTransport()
    transport.close()
    transport.close()
    with pytest.raises(ScopeSessionError):
        transport.write("*CLS")


def test_resource_redaction_hides_tcpip_host() -> None:
    assert redact_resource_name("TCPIP0::192.168.1.2::inst0::INSTR") == "TCPIP0::<redacted>::inst0::INSTR"

