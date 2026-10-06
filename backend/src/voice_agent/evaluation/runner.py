"""Provider-independent evaluation runner (docs/11 §14, docs/16 §7-§8, docs/14 §18).

Lifecycle: validate the frozen dataset and holdout policy -> create the
immutable run (idempotent on ``client_request_id``) -> ``validating`` ->
``running`` -> for each selected case and repetition: reserve, start,
execute through the layer executor, score, finalize -> ``completed`` (offline
fixture evidence needs no human rating) or ``awaiting_human_review``.

- Execution is sequential (bounded concurrency 1) with a per-slot timeout.
- Only a harness/test-setup failure invalidates a sample; it is rerun as the
  next attempt of the same slot (bounded by ``runner_retry_limit``).
  Application/provider failures are measured outcomes and never rerun.
- Live voice slots are never reserved without a live executor: they are
  reported as ``pending_live_approval``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import (
    LAYER_REPETITIONS,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
)
from voice_agent.domain.evaluation.dataset import DatasetStatus, EvaluationDataset
from voice_agent.domain.evaluation.result import (
    EvaluationResult,
    EvidenceReferences,
    HumanReviewSummary,
    InvalidationSource,
    OutputEvidence,
    ResultMeasurements,
    ResultStatus,
    UsageAndCost,
    Validity,
)
from voice_agent.domain.evaluation.run import (
    ConfigurationSnapshot,
    DatasetSnapshot,
    EvaluationRun,
    ExecutionPolicy,
    RunFailure,
    RunProgress,
    RunStatus,
)
from voice_agent.evaluation.codes import AGGREGATION_VERSION, METRIC_SCHEMA_VERSION, Reason
from voice_agent.evaluation.gates import LIVE_PROVIDER, OFFLINE_FIXTURE, gate_snapshot
from voice_agent.evaluation.observations import (
    HarnessError,
    LiveVoiceExecutor,
    ReliabilityExecutor,
    TranscriptExecutor,
)
from voice_agent.evaluation.scoring import Scored, score_live, score_reliability, score_transcript
from voice_agent.evaluation.selection import (
    RunSelection,
    loadable_splits,
    select_cases,
    subset_checksum,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.evaluation import (
    EvaluationCaseRepository,
    EvaluationDatasetRepository,
    EvaluationResultRepository,
    EvaluationRunRepository,
)
from voice_agent.ports.persistence import MAX_QUERY_LIMIT
from voice_agent.response_segmentation.disclosure import DisclosureGuard

DEFAULT_SLOT_TIMEOUT_S: Final = 120.0
MAX_SELECTED_CASE_IDS: Final = 100


class RunConfigurationError(DomainRuleError):
    """The requested run cannot start (dataset, executor, or policy problem)."""


@dataclass(frozen=True, slots=True)
class EvaluationRepositories:
    datasets: EvaluationDatasetRepository
    cases: EvaluationCaseRepository
    runs: EvaluationRunRepository
    results: EvaluationResultRepository


@dataclass(frozen=True, slots=True)
class Executors:
    transcript: TranscriptExecutor | None = None
    reliability: ReliabilityExecutor | None = None
    live: LiveVoiceExecutor | None = None  # only after separately approved live spend


@dataclass(frozen=True, slots=True)
class RunRequest:
    client_request_id: str
    name: str
    purpose: EvaluationPurpose
    environment: EvaluationEnvironment
    initiated_by: str
    configuration: ConfigurationSnapshot
    dataset_key: str
    dataset_version: int
    selection: RunSelection = field(default_factory=RunSelection)
    stop_on_critical: bool = False
    runner_retry_limit: int = 1
    spend_cap_inr: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run: EvaluationRun
    executed_slots: int
    invalid_attempts: int
    pending_live_slots: int
    stopped_on_critical: bool


@dataclass
class _Progress:
    expected: int
    completed: int = 0
    failed: int = 0
    invalid: int = 0
    cancelled: int = 0
    exhausted: int = 0  # slots whose every attempt was harness-invalid

    def snapshot(self) -> RunProgress:
        done = self.completed + self.failed + self.cancelled + self.exhausted
        return RunProgress(
            expected=self.expected,
            completed=self.completed,
            failed=self.failed,
            invalid=self.invalid,
            cancelled=self.cancelled,
            pending=max(self.expected - done, 0),
        )


class EvaluationRunner:
    def __init__(
        self,
        repositories: EvaluationRepositories,
        executors: Executors,
        *,
        guard: DisclosureGuard,
        clock: Clock,
        ids: IdGenerator,
        slot_timeout_s: float = DEFAULT_SLOT_TIMEOUT_S,
    ) -> None:
        self._repos = repositories
        self._executors = executors
        self._guard = guard
        self._clock = clock
        self._ids = ids
        self._slot_timeout_s = slot_timeout_s

    @property
    def evidence_basis(self) -> str:
        """``live_provider`` only when every model-facing executor uses real providers.

        Reliability cases are deterministic mocks by design (docs/17 §18) and
        do not change the basis.
        """
        bases = []
        if self._executors.transcript is not None:
            bases.append(self._executors.transcript.evidence_basis)
        if self._executors.live is not None:
            bases.append(LIVE_PROVIDER)
        live = bool(bases) and all(basis == LIVE_PROVIDER for basis in bases)
        return LIVE_PROVIDER if live else OFFLINE_FIXTURE

    # ------------------------------------------------------------ setup --
    async def _dataset(self, request: RunRequest) -> EvaluationDataset:
        dataset = await self._repos.datasets.get_by_key_version(
            request.dataset_key, request.dataset_version
        )
        if dataset is None or dataset.status is not DatasetStatus.FROZEN:
            raise RunConfigurationError("a run needs the frozen dataset version")
        return dataset

    async def _load_cases(
        self, dataset: EvaluationDataset, request: RunRequest
    ) -> tuple[EvaluationCase, ...]:
        loaded: list[EvaluationCase] = []
        for split in loadable_splits(request.selection, request.purpose):
            after = 0
            while True:
                page = await self._repos.cases.load_for_runner(
                    dataset.evaluation_dataset_id,
                    split=split,
                    after_sequence=after,
                    limit=MAX_QUERY_LIMIT,
                )
                loaded.extend(page)
                if len(page) < MAX_QUERY_LIMIT:
                    break
                after = page[-1].sequence_number
        return select_cases(loaded, request.selection, request.purpose)

    def _executable_layers(self, cases: Sequence[EvaluationCase]) -> tuple[EvaluationLayer, ...]:
        available = {
            EvaluationLayer.TRANSCRIPT_LLM: self._executors.transcript is not None,
            EvaluationLayer.RELIABILITY_FAILURE: self._executors.reliability is not None,
            EvaluationLayer.LIVE_VOICE: self._executors.live is not None,
        }
        layers = []
        for layer in EvaluationLayer:
            if not any(case.layer is layer for case in cases):
                continue
            if available[layer]:
                layers.append(layer)
            elif layer is not EvaluationLayer.LIVE_VOICE:
                raise RunConfigurationError(f"no executor for the {layer.value} layer")
        if not layers:
            raise RunConfigurationError("nothing executable is selected")
        return tuple(layers)

    def _build_run(
        self,
        request: RunRequest,
        dataset: EvaluationDataset,
        cases: Sequence[EvaluationCase],
        layers: tuple[EvaluationLayer, ...],
    ) -> EvaluationRun:
        now = self._clock.utc_now()
        executable = [case for case in cases if case.layer in layers]
        expected = sum(LAYER_REPETITIONS[case.layer] for case in executable)
        splits = tuple(sorted({case.split for case in cases}, key=lambda s: s.value)) or (
            EvaluationSplit.DEVELOPMENT,
        )
        subset = len(executable) != dataset.composition.total_case_count
        case_ids = tuple(c.evaluation_case_id for c in executable) if subset else ()
        return EvaluationRun(
            evaluation_run_id=self._ids.new_id(),
            client_request_id=request.client_request_id,
            name=request.name,
            purpose=request.purpose,
            status=RunStatus.QUEUED,
            status_revision=0,
            dataset_snapshot=DatasetSnapshot(
                evaluation_dataset_id=dataset.evaluation_dataset_id,
                dataset_key=dataset.dataset_key,
                version=dataset.version,
                case_set_checksum=dataset.case_set_checksum or "",
                total_case_count=dataset.composition.total_case_count,
                layer_counts=dataset.composition.layer_counts,
                split_counts=dataset.composition.split_counts,
                selected_splits=splits,
                case_subset_checksum=subset_checksum(executable) if subset else None,
                holdout_access=EvaluationSplit.HOLDOUT in splits,
            ),
            configuration_snapshot=self._configuration(request.configuration),
            execution_policy=ExecutionPolicy(
                selected_layers=layers,
                selected_splits=splits,
                selected_case_ids=case_ids[:MAX_SELECTED_CASE_IDS],
                expected_slot_count=expected,
                requested_concurrency=1,
                actual_concurrency=1,
                runner_retry_limit=request.runner_retry_limit,
                order_policy="sequence_then_repetition",
                live_case_manual_execution=True,
                invalid_sample_policy="rerun_harness_or_test_setup_only",
                stop_on_critical=request.stop_on_critical,
                spend_cap_inr=request.spend_cap_inr,
            ),
            gate_snapshot=gate_snapshot(),
            progress=RunProgress(expected=expected, pending=expected),
            environment=request.environment,
            initiated_by=request.initiated_by,
            created_at=now,
            updated_at=now,
            status_changed_at=now,
        )

    def _configuration(self, snapshot: ConfigurationSnapshot) -> ConfigurationSnapshot:
        options = {**(snapshot.safe_runtime_options or {}), "evidence_basis": self.evidence_basis}
        return snapshot.model_copy(update={"safe_runtime_options": options})

    # -------------------------------------------------------------- run --
    async def run(self, request: RunRequest) -> RunOutcome:
        dataset = await self._dataset(request)
        cases = await self._load_cases(dataset, request)
        layers = self._executable_layers(cases)
        run = await self._repos.runs.create(self._build_run(request, dataset, cases, layers))
        if run.status is not RunStatus.QUEUED:
            raise RunConfigurationError("this client request already started a run")
        run = await self._advance(run, RunStatus.VALIDATING)
        run = await self._advance(run, RunStatus.RUNNING)
        progress = _Progress(expected=run.progress.expected)
        executable = [case for case in cases if case.layer in layers]
        try:
            run, stopped = await self._execute_all(
                run, executable, progress, request.stop_on_critical
            )
        except Exception:
            await self._abort(run.evaluation_run_id)
            raise
        final = await self._finish(run, stopped)
        pending = sum(1 for case in cases if case.layer not in layers)
        executed = progress.completed + progress.failed
        return RunOutcome(final, executed, progress.invalid, pending, stopped)

    async def _execute_all(
        self,
        run: EvaluationRun,
        cases: Sequence[EvaluationCase],
        progress: _Progress,
        stop_on_critical: bool,
    ) -> tuple[EvaluationRun, bool]:
        stopped = False
        for case in cases:
            for repetition in range(1, LAYER_REPETITIONS[case.layer] + 1):
                if stopped:
                    progress.cancelled += 1  # never reserved: the run stopped first
                    continue
                critical = await self._slot(run, case, repetition, progress)
                stopped = critical and stop_on_critical
                run = await self._save_progress(run, progress)
        if progress.cancelled:
            run = await self._save_progress(run, progress)
        return run, stopped

    async def _abort(self, evaluation_run_id: str) -> None:
        """An unexpected runner error ends the run ``failed``; open attempts become cancelled."""
        current = await self._repos.runs.get(evaluation_run_id)
        if current is None or current.is_terminal:
            return
        failure = RunFailure(
            failure_code="runner_error",
            message_safe="The evaluation runner stopped on an unexpected error.",
            occurred_at=self._clock.utc_now(),
        )
        await self._advance(current, RunStatus.FAILED, failure)

    async def _save_progress(self, run: EvaluationRun, progress: _Progress) -> EvaluationRun:
        return await self._repos.runs.update_progress(
            run.evaluation_run_id,
            progress.snapshot(),
            expected_revision=run.status_revision,
            now=self._clock.utc_now(),
        )

    async def _advance(
        self, run: EvaluationRun, target: RunStatus, failure: RunFailure | None = None
    ) -> EvaluationRun:
        return await self._repos.runs.transition(
            run.evaluation_run_id,
            target,
            expected_revision=run.status_revision,
            now=self._clock.utc_now(),
            failure=failure,
        )

    async def _finish(self, run: EvaluationRun, stopped: bool) -> EvaluationRun:
        if stopped:
            failure = RunFailure(
                failure_code="stopped_on_critical",
                message_safe="The run stopped at the first critical failure (stop_on_critical).",
                occurred_at=self._clock.utc_now(),
            )
            return await self._advance(run, RunStatus.FAILED, failure)
        review = self.evidence_basis == LIVE_PROVIDER
        return await self._advance(
            run, RunStatus.AWAITING_HUMAN_REVIEW if review else RunStatus.COMPLETED
        )

    # ------------------------------------------------------------- slot --
    def _new_result(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        repetition: int,
        *,
        attempt: int = 1,
        supersedes: str | None = None,
    ) -> EvaluationResult:
        now = self._clock.utc_now()
        reviewers = 1 if self.evidence_basis == LIVE_PROVIDER else 0
        return EvaluationResult(
            evaluation_result_id=self._ids.new_id(),
            evaluation_run_id=run.evaluation_run_id,
            evaluation_dataset_id=case.evaluation_dataset_id,
            evaluation_case_id=case.evaluation_case_id,
            case_key=case.case_key,
            case_sequence_number=case.sequence_number,
            layer=case.layer,
            category=case.category,
            severity=case.severity,
            split=case.split,
            repetition_index=repetition,
            attempt_index=attempt,
            is_current_attempt=True,
            supersedes_evaluation_result_id=supersedes,
            status=ResultStatus.PENDING,
            result_revision=0,
            validity=Validity(is_valid_sample=True),
            evidence_references=EvidenceReferences(),
            output_evidence=OutputEvidence(
                mode="embedded_safe_text"
                if case.layer is EvaluationLayer.TRANSCRIPT_LLM
                else "session_references"
            ),
            measurements=ResultMeasurements(metric_schema_version=METRIC_SCHEMA_VERSION),
            usage_and_cost=UsageAndCost(
                rate_card_id=run.configuration_snapshot.rate_card_id,
                usage_status="unavailable",
                calculation_status="pending",
                reconciliation_status="not_checked",
            ),
            human_review_summary=HumanReviewSummary(
                review_status="pending" if reviewers else "not_required",
                expected_reviewer_count=reviewers,
                aggregation_version=AGGREGATION_VERSION,
            ),
            environment=run.environment,
            created_at=now,
            updated_at=now,
        )

    async def _slot(
        self, run: EvaluationRun, case: EvaluationCase, repetition: int, progress: _Progress
    ) -> bool:
        """Execute one logical slot (with harness reruns); ``True`` on a critical failure."""
        result = self._new_result(run, case, repetition)
        await self._repos.results.reserve(result)
        attempts_left = run.execution_policy.runner_retry_limit
        while True:
            started = result.start(now=self._clock.utc_now())
            await self._repos.results.save_progress(
                started, expected_revision=result.result_revision
            )
            try:
                scored = await asyncio.wait_for(
                    self._execute(case, repetition), self._slot_timeout_s
                )
            except (HarnessError, TimeoutError) as error:
                progress.invalid += 1
                invalid = await self._invalidate(started, error)
                if attempts_left <= 0:
                    progress.exhausted += 1
                    return False
                attempts_left -= 1
                result = await self._rerun(run, case, repetition, invalid)
                continue
            final = started.finalize(
                status=scored.status, now=self._clock.utc_now(), **scored.evidence()
            )
            await self._repos.results.save_progress(
                final, expected_revision=started.result_revision
            )
            if scored.status is ResultStatus.FAILED:
                progress.failed += 1
            else:
                progress.completed += 1
            return bool(scored.critical_failures)

    async def _execute(self, case: EvaluationCase, repetition: int) -> Scored:
        now = self._clock.utc_now
        if case.layer is EvaluationLayer.TRANSCRIPT_LLM and self._executors.transcript:
            observed = await self._executors.transcript.execute(case, repetition)
            return score_transcript(case, observed, guard=self._guard, now=now())
        if case.layer is EvaluationLayer.RELIABILITY_FAILURE and self._executors.reliability:
            reliability = await self._executors.reliability.execute(case, repetition)
            return score_reliability(case, reliability, now=now())
        if case.layer is EvaluationLayer.LIVE_VOICE and self._executors.live:
            live = await self._executors.live.execute(case, repetition)
            return score_live(case, live, guard=self._guard, now=now())
        raise HarnessError(Reason.TEST_SETUP_INVALID.value, "no executor for this layer")

    async def _invalidate(self, started: EvaluationResult, error: Exception) -> EvaluationResult:
        reason = getattr(error, "reason_code", Reason.HARNESS_INVALID.value)
        source = (
            InvalidationSource.TEST_SETUP
            if reason == Reason.TEST_SETUP_INVALID.value
            else InvalidationSource.HARNESS
        )
        validity = Validity(
            is_valid_sample=False,
            reason_code=reason,
            invalidation_source=source,
            counts_in_quality=False,
            counts_in_reliability=False,
            counts_in_latency=False,
            counts_in_cost=False,
            invalidated_at=self._clock.utc_now(),
            invalidated_by="evaluation-runner",
        )
        return await self._repos.results.mark_invalid(
            started.evaluation_result_id,
            validity,
            expected_revision=started.result_revision,
            now=self._clock.utc_now(),
        )

    async def _rerun(
        self, run: EvaluationRun, case: EvaluationCase, repetition: int, invalid: EvaluationResult
    ) -> EvaluationResult:
        replacement = self._new_result(
            run,
            case,
            repetition,
            attempt=invalid.attempt_index + 1,
            supersedes=invalid.evaluation_result_id,
        )
        await self._repos.results.rerun(
            invalid.evaluation_result_id,
            expected_revision=invalid.result_revision,
            replacement=replacement,
        )
        return replacement
