"""The 10 reliability/failure cases, exactly as approved in docs/17 §18-§19.

Setup/trigger, expected outcome, split, severity, and human scope are
verbatim. Each docs/17 "deterministic pass condition" becomes one allowlisted
:class:`~voice_agent.evaluation.codes.Condition`. ``S-LIFECYCLE``, ``S-COST``
and ``S-NOAUDIO`` are mandatory for every case; ``S-NOSTALE``/``S-LATENCY``/
``S-GREETING1`` are added where applicable (docs/17 §18).

``REL-091``/``REL-092`` are mock correctness checks, never latency samples:
any recorded interruption-to-silence is diagnostic and must stay <= 1,000 ms;
the P95 <= 500 ms gate uses ``INT-LIVE`` (docs/17 §19A).
"""

from __future__ import annotations

from typing import Final

from voice_agent.contracts.policies import RetryPolicy
from voice_agent.evaluation.catalog_rows import ReliabilityRow as R
from voice_agent.evaluation.codes import Condition as Cond
from voice_agent.turn_management.fallbacks import (
    RESPONSE_FAILED,
    SESSION_TIME_LIMIT,
    UNCLEAR_INPUT,
)
from voice_agent.turn_management.greeting import OPENING_GREETING

_BASE: Final = ("S-LIFECYCLE", "S-COST", "S-NOAUDIO")
INTERRUPTION_DIAGNOSTIC_MAX_MS: Final = 1000
# "According to the configured limit" (docs/17 §19): the approved RetryPolicy default.
CONFIGURED_MAX_ATTEMPTS: Final = RetryPolicy().maximum_attempts

RELIABILITY_ROWS: Final[tuple[R, ...]] = (
    R(
        91,
        "D",
        "critical",
        "interruption",
        "Start a normal turn. As soon as browser playback-start acknowledges the first agent "
        "segment, the user says: `रुको, पहले मेरी नई बात सुनो।`",
        "Accept barge-in, stop old audio, process the new final transcript, and never resume "
        "the old response.",
        "barge_in_first_segment",
        "turn_management",
        "first_playback_start",
        "new_turn_responds_once",
        (
            Cond.OLD_GENERATION_CANCELLED,
            Cond.NO_STALE_AUDIO,
            Cond.NO_STALE_HISTORY,
            Cond.SINGLE_NEW_RESPONSE,
            Cond.INTERRUPTION_WITHIN_MAX,
        ),
        (*_BASE, "S-NOSTALE", "S-LATENCY"),
        "H-VOICE",
        "hi",
        {
            "barge_in_utterance": "रुको, पहले मेरी नई बात सुनो।",
            "interruption_max_ms": INTERRUPTION_DIAGNOSTIC_MAX_MS,
        },
    ),
    R(
        92,
        "H",
        "critical",
        "interruption",
        "During the middle of a multi-segment answer, the user says: `Stop. Give me only a one "
        "sentence summary.`",
        "Stop remaining segments and produce one concise English summary for the new turn.",
        "barge_in_mid_response",
        "turn_management",
        "mid_response_segment",
        "new_turn_responds_once",
        (
            Cond.OLD_GENERATION_CANCELLED,
            Cond.NO_STALE_AUDIO,
            Cond.NO_STALE_HISTORY,
            Cond.SINGLE_NEW_RESPONSE,
            Cond.ONE_SENTENCE_RESPONSE,
            Cond.INTERRUPTION_WITHIN_MAX,
        ),
        (*_BASE, "S-NOSTALE", "S-LATENCY"),
        "H-VOICE",
        "en",
        {
            "barge_in_utterance": "Stop. Give me only a one sentence summary.",
            "interruption_max_ms": INTERRUPTION_DIAGNOSTIC_MAX_MS,
        },
    ),
    R(
        93,
        "D",
        "high",
        "interruption",
        "During agent playback, create a sub-250 ms non-speech disturbance such as one "
        "cough/tap and provide no substantive user utterance.",
        "Suppress the false-interruption candidate without pausing/cancelling playback, "
        "starting a turn, or duplicating speech.",
        "false_interruption_short_noise",
        "speech_activity",
        "during_playback",
        "playback_continues",
        (
            Cond.NO_ACCEPTED_INTERRUPTION,
            Cond.NO_NEW_LLM_REQUEST,
            Cond.PLAYBACK_CONTINUED_ONCE,
            Cond.SUPPRESSION_EVIDENCED,
        ),
        _BASE,
        "H-VOICE",
        "hinglish",
        {"disturbance_ms": 200},
    ),
    R(
        94,
        "H",
        "critical",
        "reconnect",
        "Disconnect the browser network during active agent playback, wait five seconds, then "
        "reconnect within the 20-second reconnect window and send valid `client.ready`.",
        "Stop speech on disconnect, reject stale output, restore permitted session state, and "
        "do not replay the opening greeting.",
        "disconnect_during_playback",
        "transport",
        "active_playback",
        "resume_without_greeting",
        (
            Cond.NO_STALE_AUDIO,
            Cond.OLD_GENERATION_CANCELLED,
            Cond.SINGLE_GREETING,
            Cond.RECONNECT_RECORDED,
            Cond.NO_DUPLICATE_TURN,
        ),
        (*_BASE, "S-NOSTALE", "S-LATENCY", "S-GREETING1"),
        "H-VOICE",
        "hinglish",
        {"disconnect_ms": 5000, "reconnect_window_ms": 20000},
    ),
    R(
        95,
        "D",
        "critical",
        "reconnect",
        "Disconnect the browser after an accepted final transcript but before the LLM "
        "completes. Reconnect within the approved window.",
        "Late model/TTS output must not become audible; session either safely resumes with a "
        "newly authorized action or reports normalized state without duplication.",
        "disconnect_before_llm_complete",
        "transport",
        "after_final_transcript",
        "one_terminal_disposition",
        (
            Cond.OLD_GENERATION_CANCELLED,
            Cond.NO_STALE_AUDIO,
            Cond.NO_STALE_HISTORY,
            Cond.ONE_TERMINAL_DISPOSITION,
        ),
        (*_BASE, "S-NOSTALE"),
        "H-VOICE",
        "hinglish",
        {"reconnect_window_ms": 20000},
    ),
    R(
        96,
        "D",
        "high",
        "provider_failure",
        "STT fault adapter receives user audio but emits no usable final transcript until the "
        "3,000 ms finalization timeout.",
        f"Use the deterministic unclear-speech fallback once: `{UNCLEAR_INPUT.text}`",
        "stt_finalization_timeout",
        "stt",
        "turn_finalization",
        "unclear_input_fallback_once",
        (Cond.NO_LLM_REQUEST, Cond.FALLBACK_ONCE, Cond.SINGLE_TTS_ATTEMPT, Cond.NORMALIZED_FAILURE),
        _BASE,
        "H-VOICE",
        "hinglish",
        {"finalization_timeout_ms": 3000, "fallback_template_id": UNCLEAR_INPUT.template_id},
    ),
    R(
        97,
        "D",
        "high",
        "provider_failure",
        "LLM fault adapter accepts a valid transcript, then times out/fails before any "
        "authorized speakable segment.",
        f"Speak once: `{RESPONSE_FAILED.text}`",
        "llm_failure_before_segment",
        "conversation_engine",
        "before_first_segment",
        "response_failed_fallback_once",
        (Cond.FALLBACK_ONCE, Cond.BOUNDED_RETRIES, Cond.NORMALIZED_FAILURE, Cond.NO_STALE_AUDIO),
        (*_BASE, "S-NOSTALE"),
        "H-VOICE",
        "hinglish",
        {
            "fallback_template_id": RESPONSE_FAILED.template_id,
            "max_attempts": CONFIGURED_MAX_ATTEMPTS,
        },
    ),
    R(
        98,
        "D",
        "critical",
        "provider_failure",
        "TTS fault adapter fails before producing playable audio for an otherwise valid LLM "
        "response and also rejects the first retry according to the configured limit.",
        "Show normalized safe UI failure/text when available; do not recursively request "
        "spoken fallback through the failed TTS path.",
        "tts_failure_before_audio",
        "tts",
        "before_first_audio",
        "safe_failure_without_recursion",
        (
            Cond.NO_RECURSIVE_TTS,
            Cond.TEXT_NOT_DELIVERED,
            Cond.NORMALIZED_FAILURE,
            Cond.NO_STALE_AUDIO,
        ),
        (*_BASE, "S-NOSTALE"),
        "H-BASE",
        "hinglish",
        {"max_tts_attempts": CONFIGURED_MAX_ATTEMPTS},
    ),
    R(
        99,
        "D",
        "critical",
        "greeting",
        "Create a new session, send valid `client.ready` twice, disconnect/reconnect once, and "
        "send `client.ready` again.",
        "The application-owned greeting plays exactly once for the entire session.",
        "duplicate_ready_and_reconnect",
        "session",
        "client_ready",
        "greeting_exactly_once",
        (Cond.SINGLE_GREETING, Cond.NO_GREETING_LLM_CALL),
        (*_BASE, "S-GREETING1"),
        "H-VOICE",
        "hinglish",
        {"greeting_template_id": OPENING_GREETING.template_id},
    ),
    R(
        100,
        "D",
        "critical",
        "time_limit",
        "Use the safe test clock to reach the approved 30-minute maximum session duration "
        "while the transport is deliverable.",
        f"Speak once: `{SESSION_TIME_LIMIT.text}` Then finalize the session.",
        "maximum_session_duration",
        "session",
        "maximum_duration",
        "time_limit_notice_then_end",
        (Cond.FALLBACK_ONCE, Cond.NO_TURN_AFTER_LIMIT, Cond.SESSION_ENDED, Cond.NO_LLM_REQUEST),
        _BASE,
        "H-VOICE",
        "hinglish",
        {"maximum_duration_s": 1800, "fallback_template_id": SESSION_TIME_LIMIT.template_id},
    ),
)
