"""WP1 exit gate: the Silero VAD model loads locally with network access blocked.

docs/14 §7 and docs/13 §14 item 2 require the ``livekit-agents[silero]``
dependencies (including ``onnxruntime``) to install from prebuilt wheels and
the bundled Silero model to load without a download or source build.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import numpy as np
import pytest

SAMPLE_RATE_HZ = 16_000
SILENCE_SPEECH_PROBABILITY_CEILING = 0.5


class NetworkAccessBlockedError(RuntimeError):
    """Raised when code under test attempts a network connection."""


@pytest.fixture
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise NetworkAccessBlockedError("network access is blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


def test_network_guard_is_effective(block_network: None) -> None:
    with pytest.raises(NetworkAccessBlockedError):
        socket.create_connection(("pypi.org", 443), timeout=1)


@pytest.mark.local_model
def test_silero_vad_loads_bundled_model_offline(block_network: None) -> None:
    from livekit.plugins import silero

    vad = silero.VAD.load(sample_rate=SAMPLE_RATE_HZ, force_cpu=True)

    assert isinstance(vad, silero.VAD)


@pytest.mark.local_model
def test_bundled_silero_model_runs_inference_on_silence(block_network: None) -> None:
    import onnxruntime
    from livekit.plugins.silero import onnx_model

    session = onnx_model.new_inference_session(force_cpu=True)
    model = onnx_model.OnnxModel(onnx_session=session, sample_rate=SAMPLE_RATE_HZ)
    silence = np.zeros(model.window_size_samples, dtype=np.float32)

    probability = model(silence)

    assert "CPUExecutionProvider" in onnxruntime.get_available_providers()
    assert 0.0 <= probability < SILENCE_SPEECH_PROBABILITY_CEILING
