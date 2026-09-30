"""Silero stand-ins for worker tests: speech probability from the window's mean level."""

from __future__ import annotations

import numpy as np

from voice_agent.speech_activity.silero import SILERO_WINDOW_SAMPLES


class EnergyModel:
    window_size_samples = SILERO_WINDOW_SAMPLES

    def __call__(self, window: np.ndarray) -> float:
        return float(min(1.0, np.abs(window).mean() * 4))

    def reset(self) -> None:
        return None


class EnergyHandle:
    """Stands in for the prewarmed ``SileroModelHandle``."""

    loads = 1

    def new_model(self) -> EnergyModel:
        return EnergyModel()
