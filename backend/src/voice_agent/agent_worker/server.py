"""Local LiveKit agent-worker process (docs/14 §21; docs/05 §2, §21).

``python -m voice_agent.agent_worker [--media-mode tone|echo|stt|llm|tts]``

``stt`` (WP7) runs local Silero VAD + the Turn Manager + Deepgram streaming
STT, with no LLM or TTS, for sessions whose approved configuration has the
Deepgram section; the Silero model is loaded once here, before registration.
``llm`` (WP8) adds one GPT-6 Luna generation per accepted turn, published as
``va.response.v1`` text (no TTS), for the approved OpenAI configuration.

Startup: validate bootstrap settings through the WP3 loader and the worker
readiness check (MongoDB + LiveKit credentials, default configuration),
then register with LiveKit for explicit dispatch under ``APP_AGENT_NAME``.
The health endpoint binds to ``127.0.0.1`` only. Logs are safe JSON: the
``livekit`` SDK loggers are limited to warnings and rendered without extras,
so no URL, key, token, or secret is printed. Ctrl+C drains and closes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from voice_agent.agent_worker.entrypoint import (
    SPEECH_MODES,
    WorkerConfig,
    handle_request,
    new_worker_instance_id,
    run_job,
)
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.control_api.structured_logging import SafeJsonFormatter
from voice_agent.provider_registry.startup_check import StartupOutcome, run_startup_check
from voice_agent.security.readiness import PersistenceMode, ProcessRole
from voice_agent.speech_activity.silero import SileroModelHandle

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
HEALTH_HOST: Final = "127.0.0.1"
HEALTH_PORT: Final = 8081
LOAD_THRESHOLD: Final = 0.95
SDK_LOGGERS: Final = (
    "livekit",
    "livekit.agents",
    "livekit.rtc",
    "livekit.api",
    "livekit.plugins.silero",
    "deepgram",
    "websockets",
)
_CONFIG: dict[str, WorkerConfig] = {}


def configure_worker_logging(level: str) -> None:
    root = logging.getLogger()
    if not any(isinstance(h.formatter, SafeJsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(SafeJsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)
    for name in SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Phase 0 LiveKit agent worker.")
    parser.add_argument(
        "--media-mode", choices=[mode.value for mode in MediaMode], default=MediaMode.TONE.value
    )
    return parser


def _refuse(payload: Mapping[str, object]) -> int:
    sys.stderr.write(json.dumps({"started": False, **payload}, sort_keys=True) + "\n")
    return EXIT_REFUSED


def _startup(environ: Mapping[str, str] | None) -> StartupOutcome:
    return run_startup_check(
        ProcessRole.AGENT_WORKER, persistence=PersistenceMode.MONGODB, environ=environ
    )


def _config() -> WorkerConfig:
    return _CONFIG["worker"]


def install_config(config: WorkerConfig) -> None:
    """Process-wide worker configuration read by the job threads."""
    _CONFIG["worker"] = config


async def _on_request(request: Any) -> None:
    await handle_request(request, _config())


async def _entrypoint(ctx: Any) -> None:
    await run_job(ctx, _config())


def build_server(config: WorkerConfig, *, health_port: int = HEALTH_PORT) -> Any:
    """Construct the ``AgentServer`` for explicit named dispatch (credentials explicit)."""
    from livekit import agents

    settings = config.settings
    key, secret = settings.livekit_api_key, settings.livekit_api_secret
    if settings.livekit_url is None or key is None or secret is None:  # pragma: no cover
        raise RuntimeError("LiveKit is not configured")
    server = agents.AgentServer(
        ws_url=settings.livekit_url,
        api_key=key.get_secret_value(),
        api_secret=secret.get_secret_value(),
        host=HEALTH_HOST,
        port=health_port,
        num_idle_processes=1,
        # One local R&D session: stay available unless the host is saturated.
        load_threshold=LOAD_THRESHOLD,
        job_executor_type=agents.JobExecutorType.THREAD,
        permissions=agents.WorkerPermissions(can_update_metadata=False),
    )
    server.rtc_session(_entrypoint, agent_name=settings.app_agent_name, on_request=_on_request)
    server.on("worker_registered", _registered)
    return server


def _registered(*_args: Any) -> None:
    """Safe registration evidence (the SDK's own record carries the server URL)."""
    logging.getLogger("voice_agent.agent_worker").info("worker.registered")


async def _serve(server: Any) -> None:
    try:
        await server.run(devmode=False)
    finally:
        await server.aclose()


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    serve: Callable[[Any], None] = lambda server: asyncio.run(_serve(server)),
    prewarm: Callable[[], SileroModelHandle] = SileroModelHandle.load,
) -> int:
    args = _parser().parse_args(argv)
    outcome = _startup(environ)
    if not outcome.report.ready or outcome.loaded is None:
        return _refuse(outcome.report.to_safe_dict())
    settings = outcome.loaded.settings
    configure_worker_logging(settings.app_log_level)
    mode = MediaMode(args.media_mode)
    silero = prewarm() if mode in SPEECH_MODES else None
    if silero is not None:
        logging.getLogger("voice_agent.agent_worker").info("worker.silero_prewarmed")
    config = WorkerConfig(
        settings=settings,
        media_mode=mode,
        worker_instance_id=new_worker_instance_id(),
        silero=silero,
    )
    install_config(config)
    serve(build_server(config))
    return EXIT_OK
