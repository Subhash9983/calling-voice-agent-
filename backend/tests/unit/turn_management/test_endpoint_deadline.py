"""VAD silence window and endpoint deadline are not additive (docs/07 §10, Decision 067).

Silero (scripted probabilities) reports speech stop ~550 ms after the last
speech window; the Turn Manager then waits only the *remaining* time, so the
endpoint commits 700 ms after the last speech frame, not 550 + 700 = 1,250 ms.
"""

from __future__ import annotations

import numpy as np
import pytest

from voice_agent.contracts.audio import AudioFrame, square_wave_pcm
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityKind
from voice_agent.speech_activity.silero import SILERO_WINDOW_SAMPLES, SileroSpeechActivityDetector
from voice_agent.turn_management.turn_manager import (
    CommitEndpoint,
    OpenTurn,
    TurnTakingState,
    on_speech,
    on_tick,
)

SESSION = "00000000-0000-4000-8000-000000000001"


class LevelModel:
    window_size_samples = SILERO_WINDOW_SAMPLES

    def __call__(self, window: np.ndarray) -> float:
        return 0.9 if np.abs(window).max() > 0.1 else 0.05

    def reset(self) -> None:
        return None


def _frame(sequence: int, level: float) -> AudioFrame:
    return AudioFrame(
        session_id=SESSION,
        sequence=sequence,
        sample_rate_hz=16_000,
        duration_ms=20,
        captured_at_ms=sequence * 20,
        pcm=square_wave_pcm(level, 320),
    )


@pytest.mark.parametrize("deadline_ms", [700, 850, 1000])
def test_endpoint_commits_at_last_speech_plus_total_deadline(deadline_ms: int) -> None:
    policy = TurnHandlingPolicy(endpoint_deadline_ms=deadline_ms)
    detector = SileroSpeechActivityDetector(LevelModel(), policy)
    state = TurnTakingState()
    stop_at: int | None = None
    last_speech: int | None = None
    commits: list[CommitEndpoint] = []
    opened = 0
    for sequence in range(150):  # 1 s speech, then 2 s silence
        frame = _frame(sequence, 0.5 if sequence < 50 else 0.0)
        for activity in detector.process(frame):
            if activity.kind is SpeechActivityKind.SPEECH_STOPPED:
                stop_at, last_speech = activity.at_ms, activity.last_speech_at_ms
            state, decisions = on_speech(state, activity, policy)
            opened += sum(isinstance(d, OpenTurn) for d in decisions)
        state, decisions = on_tick(state, frame.ends_at_ms, policy)
        commits.extend(d for d in decisions if isinstance(d, CommitEndpoint))

    assert opened == 1
    assert stop_at is not None
    assert last_speech is not None
    assert 550 <= stop_at - last_speech < 550 + 32  # one Silero window of granularity
    [commit] = commits
    assert commit.last_speech_at_ms == last_speech
    assert deadline_ms <= commit.committed_at_ms - last_speech < deadline_ms + 20
    assert commit.committed_at_ms - last_speech < 550 + 700  # never additive


def test_endpoint_deadline_is_capped_at_1000_ms() -> None:
    with pytest.raises(ValueError, match="less than or equal to 1000"):
        TurnHandlingPolicy(endpoint_deadline_ms=1250)
