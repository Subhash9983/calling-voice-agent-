"""WP5 evaluation repositories over the in-process Mongo fake, seeded with the docs/17 dataset."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tests.support.evaluation_reliability import RigReliabilityExecutor
from tests.support.evaluation_replies import ScriptedEngineFactory
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.persistence_builders import make_config

from voice_agent.contracts.policies import RetryPolicy
from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID, phase0_rate_card
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.evaluation.common import EvaluationEnvironment, EvaluationPurpose
from voice_agent.domain.evaluation.run import ComponentIdentity, ConfigurationSnapshot
from voice_agent.evaluation.catalog import CatalogContext
from voice_agent.evaluation.codes import (
    DATASET_KEY,
    DATASET_VERSION,
    RUBRIC_VERSION,
    RULE_VERSION,
    RUNNER_VERSION,
)
from voice_agent.evaluation.observations import LiveVoiceExecutor, ReliabilityExecutor
from voice_agent.evaluation.runner import (
    EvaluationRepositories,
    EvaluationRunner,
    Executors,
    RunRequest,
)
from voice_agent.evaluation.seeding import SeedResult, seed_catalog
from voice_agent.evaluation.selection import RunSelection
from voice_agent.evaluation.transcript_executor import (
    EngineTranscriptExecutor,
    TranscriptHarnessSetup,
)
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.bootstrap import apply_schema
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.evaluation_definitions import (
    MongoEvaluationCaseRepository,
    MongoEvaluationDatasetRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_results import (
    MongoEvaluationRatingRepository,
    MongoEvaluationResultRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_runs import (
    MongoEvaluationRunRepository,
)
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION
from voice_agent.response_segmentation.disclosure import DisclosureGuard

CHECKSUM = "sha256:" + "a" * 64


@dataclass
class EvalStores:
    database: FakeDatabase
    persistence: MongoPersistence
    repositories: EvaluationRepositories
    ratings: MongoEvaluationRatingRepository
    config: AgentConfig
    seed: SeedResult | None = None
    factories: list[ScriptedEngineFactory] = field(default_factory=list)

    @property
    def results(self) -> MongoEvaluationResultRepository:
        results = self.repositories.results
        assert isinstance(results, MongoEvaluationResultRepository)
        return results

    def snapshot(self, engine: str = "mock_llm") -> ConfigurationSnapshot:
        config = self.config

        def component(provider: str) -> ComponentIdentity:
            return ComponentIdentity(provider=provider, adapter_version="offline-harness")

        return ConfigurationSnapshot(
            agent_config_id=config.agent_config_id,
            agent_config_version=config.version,
            config_checksum=config.config_checksum,
            prompt_id=config.conversation_engine.prompt_id,
            prompt_version=config.conversation_engine.system_instruction_version,
            prompt_checksum=config.conversation_engine.prompt_checksum,
            transport=component("fake_session_transport"),
            stt=component("fake_deepgram"),
            conversation_engine=component(engine),
            tts=component("mock_tts"),
            application_version="0.12.0",
            commit_reference="wp12-offline",
            python_lock_checksum=CHECKSUM,
            frontend_lock_checksum=CHECKSUM,
            runner_version=RUNNER_VERSION,
            rule_version=RULE_VERSION,
            rubric_version=RUBRIC_VERSION,
            rate_card_id=PHASE0_RATE_CARD_ID,
            environment_label="development",
        )

    def request(
        self,
        selection: RunSelection | None = None,
        *,
        purpose: EvaluationPurpose = EvaluationPurpose.DEVELOPMENT,
        stop_on_critical: bool = False,
        client_request_id: str | None = None,
        runner_retry_limit: int = 1,
    ) -> RunRequest:
        return RunRequest(
            client_request_id=client_request_id or str(uuid.uuid4()),
            name="WP12 offline harness run",
            purpose=purpose,
            environment=EvaluationEnvironment.DEVELOPMENT,
            initiated_by="wp12-test-runner",
            configuration=self.snapshot(),
            dataset_key=DATASET_KEY,
            dataset_version=DATASET_VERSION,
            selection=selection or RunSelection(),
            stop_on_critical=stop_on_critical,
            runner_retry_limit=runner_retry_limit,
        )

    def runner(
        self,
        replies: dict[int, str] | None = None,
        *,
        reliability: ReliabilityExecutor | None = None,
        live: LiveVoiceExecutor | None = None,
        transcript: bool = True,
        evidence_basis: str = "offline_fixture",
        slot_timeout_s: float = 30,
    ) -> EvaluationRunner:
        factory = ScriptedEngineFactory(replies)
        self.factories.append(factory)
        setup = TranscriptHarnessSetup(
            agent_config_id=self.config.agent_config_id,
            config_checksum=self.config.config_checksum,
            prompt_id="phase0_general_voice_assistant_v1",
            prompt_version=1,
            system_instruction=PHASE0_SYSTEM_INSTRUCTION,
            provider="mock_llm",
            model="mock-llm-v1",
            environment=self.config.environment,
            card=phase0_rate_card(),
            evidence_basis=evidence_basis,
            retry=RetryPolicy(initial_backoff_ms=0, maximum_backoff_ms=0),
        )
        executor = EngineTranscriptExecutor(
            factory, setup, clock=SystemClock(), ids=UuidIdGenerator()
        )
        return EvaluationRunner(
            self.repositories,
            Executors(
                transcript=executor if transcript else None,
                reliability=reliability if reliability is not None else RigReliabilityExecutor(),
                live=live,
            ),
            guard=DisclosureGuard.for_instruction(PHASE0_SYSTEM_INSTRUCTION),
            clock=SystemClock(),
            ids=UuidIdGenerator(),
            slot_timeout_s=slot_timeout_s,
        )


def persistence_for(database: FakeDatabase) -> MongoPersistence:
    return MongoPersistence.from_handles(FakeClient(database), database)


async def evaluation_stores(*, seed: bool = True) -> EvalStores:
    database = FakeDatabase()
    persistence = persistence_for(database)
    await apply_schema(database)
    config = make_config()
    await MongoAgentConfigRepository(persistence).insert(config)
    repositories = EvaluationRepositories(
        datasets=MongoEvaluationDatasetRepository(persistence),
        cases=MongoEvaluationCaseRepository(persistence),
        runs=MongoEvaluationRunRepository(persistence),
        results=MongoEvaluationResultRepository(persistence),
    )
    stores = EvalStores(
        database, persistence, repositories, MongoEvaluationRatingRepository(persistence), config
    )
    if seed:
        ctx = CatalogContext(
            EvaluationEnvironment.DEVELOPMENT, "wp12-seed", datetime.now(UTC), "docs17-v1"
        )
        stores.seed = await seed_catalog(repositories.datasets, repositories.cases, ctx)
    return stores


def keys(items: Sequence[tuple[str, int]], predicate: Callable[[str], bool]) -> set[str]:
    return {key for key, _ in items if predicate(key)}
