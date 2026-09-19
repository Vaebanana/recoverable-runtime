from pathlib import Path

import pytest

from browser_use.recovery.bootstrap import RecoveryBootstrap
from browser_use.recovery.contracts import (
	EffectSpec,
	EffectStatus,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	UnitRuntimeState,
	UnitStatus,
	VerificationSource,
	VerificationSpec,
	VerificationStatus,
)
from browser_use.recovery.persistence.checkpoint import CheckpointManager
from browser_use.recovery.persistence.models import EffectRecordDraft, EffectRecordStatus, RuntimeCheckpoint, WorkflowRun
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.reconciliation import ReconciliationCoordinator, ReconciliationError
from browser_use.recovery.runtime_state import RuntimeStateManager, RuntimeStateTransitionError
from browser_use.recovery.side_effects import SideEffectCoordinator, SideEffectExecutionError
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationManager,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)


def contract() -> SemanticContract:
	"""Build one non-idempotent unit with observable postconditions."""
	unit = SemanticUnit(
		unit_id='u1',
		identity=UnitIdentity(intent_key='submit', target_key='application/7', outcome_key='submitted'),
		intent='submit application',
		target=TargetSpec(type='application', key='application/7'),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
		),
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='inspect application history'),
	)
	return SemanticContract(contract_id='contract-1', task_id='task-1', version=1, units=(unit,))


def unknown_states() -> dict[str, UnitRuntimeState]:
	"""Return the only state snapshot eligible for reconciliation."""
	return {
		'u1': UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.UNKNOWN,
			verification_status=VerificationStatus.INCONCLUSIVE,
			effect_status=EffectStatus.UNKNOWN,
		)
	}


def verification_result(semantic_contract: SemanticContract, outcome: VerificationOutcome) -> VerificationResult:
	"""Build deterministic evidence for one reconciliation observation."""
	unit = semantic_contract.get_unit('u1')
	return VerificationResult(
		outcome=outcome,
		evidence=VerificationEvidence(
			expected=expected_target_evidence(unit),
			observed={'target.key': unit.target.key} if outcome is VerificationOutcome.VERIFIED else {},
			source=VerificationSource.BROWSER,
			summary='application history inspected',
		),
	)


class StaticVerifier:
	"""Return one observational result without executing a side effect."""

	observational = True

	def __init__(self, result: VerificationResult) -> None:
		self._result = result

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		return self._result


def append_effect(
	storage: SQLiteRuntimeStorage,
	*,
	status: EffectRecordStatus,
	seq_identity: tuple[str, str],
	run_id: str = 'run-old',
) -> None:
	"""Append one test ledger fact for a stable effect attempt."""
	effect_id, attempt_id = seq_identity
	storage.append_effect(
		EffectRecordDraft(
			effect_id=effect_id,
			workflow_id='wf-1',
			run_id=run_id,
			unit_id='u1',
			effect_key='submit-application',
			attempt_id=attempt_id,
			status=status,
			idempotency_key=f'idempotency-{attempt_id}',
		)
	)


def reconciliation_coordinator(
	storage: SQLiteRuntimeStorage,
	semantic_contract: SemanticContract,
	outcome: VerificationOutcome,
) -> ReconciliationCoordinator:
	"""Build a deterministic coordinator for the restarted run."""
	return ReconciliationCoordinator(
		storage=storage,
		checkpoint_manager=CheckpointManager(storage, checkpoint_id_factory=lambda: 'cp-reconciled'),
		workflow_id='wf-1',
		run_id='run-new',
		contract=semantic_contract,
		verifier=StaticVerifier(verification_result(semantic_contract, outcome)),
	)


@pytest.mark.asyncio
async def test_observe_collects_evidence_without_changing_runtime_state() -> None:
	semantic_contract = contract()
	states = unknown_states()
	original_state = states['u1']
	manager: VerificationManager[object] = VerificationManager()

	result = await manager.observe(
		semantic_contract.get_unit('u1'),
		StaticVerifier(verification_result(semantic_contract, VerificationOutcome.VERIFIED)),
		object(),
	)

	assert result.outcome is VerificationOutcome.VERIFIED
	assert states['u1'] is original_state


@pytest.mark.parametrize(
	'state',
	[
		UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.ACTIVE,
			verification_status=VerificationStatus.INCONCLUSIVE,
			effect_status=EffectStatus.UNKNOWN,
		),
		UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.UNKNOWN,
			verification_status=VerificationStatus.NOT_CHECKED,
			effect_status=EffectStatus.UNKNOWN,
		),
		UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.UNKNOWN,
			verification_status=VerificationStatus.INCONCLUSIVE,
			effect_status=EffectStatus.ATTEMPTED,
		),
	],
)
def test_reconciliation_transition_requires_the_complete_unknown_triple(state: UnitRuntimeState) -> None:
	semantic_contract = contract()

	with pytest.raises(RuntimeStateTransitionError, match='UNKNOWN/UNKNOWN/INCONCLUSIVE'):
		RuntimeStateManager().apply_reconciliation_result(
			semantic_contract,
			{'u1': state},
			'u1',
			verification_result(semantic_contract, VerificationOutcome.REJECTED),
		)


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'outcome, expected_unit_status, expected_effect_status, expected_verification_status, expected_record_status',
	[
		(
			VerificationOutcome.VERIFIED,
			UnitStatus.COMPLETED,
			EffectStatus.COMMITTED,
			VerificationStatus.VERIFIED,
			EffectRecordStatus.COMMITTED,
		),
		(
			VerificationOutcome.REJECTED,
			UnitStatus.ACTIVE,
			EffectStatus.NOT_APPLIED,
			VerificationStatus.REJECTED,
			EffectRecordStatus.NOT_APPLIED,
		),
		(
			VerificationOutcome.INCONCLUSIVE,
			UnitStatus.UNKNOWN,
			EffectStatus.UNKNOWN,
			VerificationStatus.INCONCLUSIVE,
			EffectRecordStatus.UNKNOWN,
		),
	],
)
async def test_reconcile_closes_latest_unresolved_attempt_and_commits_checkpoint(
	tmp_path: Path,
	outcome: VerificationOutcome,
	expected_unit_status: UnitStatus,
	expected_effect_status: EffectStatus,
	expected_verification_status: VerificationStatus,
	expected_record_status: EffectRecordStatus,
) -> None:
	semantic_contract = contract()
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-new'))
		storage.save_contract('wf-1', semantic_contract)
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.ATTEMPTED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.NOT_APPLIED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-2', 'attempt-2'))
		append_effect(storage, status=EffectRecordStatus.ATTEMPTED, seq_identity=('effect-2', 'attempt-2'))

		result = await reconciliation_coordinator(storage, semantic_contract, outcome).reconcile(
			unit_id='u1',
			states=unknown_states(),
			last_effect_seq=4,
			context={'page': 'application history'},
		)

		assert result.record.status is expected_record_status
		assert result.record.effect_id == 'effect-2'
		assert result.record.attempt_id == 'attempt-2'
		assert result.record.idempotency_key == 'idempotency-attempt-2'
		assert result.record.run_id == 'run-new'
		assert result.states['u1'].status is expected_unit_status
		assert result.states['u1'].effect_status is expected_effect_status
		assert result.states['u1'].verification_status is expected_verification_status
		checkpoint = storage.load_latest_checkpoint('wf-1')
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == result.record.seq
		assert checkpoint.unit_states['u1'] == result.states['u1']


@pytest.mark.asyncio
async def test_reconcile_groups_attempts_instead_of_using_the_latest_unit_record(tmp_path: Path) -> None:
	semantic_contract = contract()
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-new'))
		storage.save_contract('wf-1', semantic_contract)
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-2', 'attempt-2'))
		append_effect(storage, status=EffectRecordStatus.ATTEMPTED, seq_identity=('effect-2', 'attempt-2'))
		append_effect(storage, status=EffectRecordStatus.COMMITTED, seq_identity=('effect-1', 'attempt-1'))

		result = await reconciliation_coordinator(
			storage,
			semantic_contract,
			VerificationOutcome.VERIFIED,
		).reconcile(
			unit_id='u1',
			states=unknown_states(),
			last_effect_seq=4,
			context={'application_exists': True},
		)

		assert result.record.effect_id == 'effect-2'
		assert result.record.attempt_id == 'attempt-2'
		assert result.record.metadata['reconciled_from_seq'] == 3


@pytest.mark.asyncio
async def test_reconcile_requires_an_unresolved_attempt(tmp_path: Path) -> None:
	semantic_contract = contract()
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-new'))
		storage.save_contract('wf-1', semantic_contract)
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.COMMITTED, seq_identity=('effect-1', 'attempt-1'))

		with pytest.raises(ReconciliationError, match='unresolved effect attempt'):
			await reconciliation_coordinator(storage, semantic_contract, VerificationOutcome.VERIFIED).reconcile(
				unit_id='u1',
				states=unknown_states(),
				last_effect_seq=1,
				context=object(),
			)


@pytest.mark.asyncio
async def test_rejected_reconciliation_allows_a_new_effect_attempt(tmp_path: Path) -> None:
	semantic_contract = contract()
	executor_calls = 0
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-new'))
		storage.save_contract('wf-1', semantic_contract)
		append_effect(storage, status=EffectRecordStatus.PREPARED, seq_identity=('effect-1', 'attempt-1'))
		append_effect(storage, status=EffectRecordStatus.ATTEMPTED, seq_identity=('effect-1', 'attempt-1'))
		reconciled = await reconciliation_coordinator(
			storage,
			semantic_contract,
			VerificationOutcome.REJECTED,
		).reconcile(
			unit_id='u1',
			states=unknown_states(),
			last_effect_seq=1,
			context={'application_exists': False},
		)
		retry = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(
				storage,
				checkpoint_id_factory=iter(['cp-retry-prepared', 'cp-retry-final']).__next__,
			),
			workflow_id='wf-1',
			run_id='run-new',
			contract=semantic_contract,
			verifier=StaticVerifier(verification_result(semantic_contract, VerificationOutcome.VERIFIED)),
			effect_id_factory=lambda: 'effect-2',
			attempt_id_factory=lambda: 'attempt-2',
		)

		async def execute_again() -> dict[str, bool]:
			nonlocal executor_calls
			executor_calls += 1
			return {'submitted': True}

		result = await retry.execute(
			unit_id='u1',
			effect_key='submit-application',
			states=dict(reconciled.states),
			last_effect_seq=reconciled.record.seq,
			executor=execute_again,
		)

		assert executor_calls == 1
		assert result.records[0].attempt_id == 'attempt-2'
		assert result.states['u1'].status is UnitStatus.COMPLETED


@pytest.mark.asyncio
async def test_crash_recovery_reconciliation_never_reexecutes_non_idempotent_effect(tmp_path: Path) -> None:
	semantic_contract = contract()
	executor_calls = 0
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.save_contract('wf-1', semantic_contract)
		prepared_checkpoint = RuntimeCheckpoint(
			checkpoint_id='cp-prepared',
			workflow_id='wf-1',
			run_id='run-old',
			contract_id='contract-1',
			contract_version=1,
			unit_states={
				'u1': UnitRuntimeState(
					unit_id='u1',
					status=UnitStatus.ACTIVE,
					effect_status=EffectStatus.NOT_STARTED,
				)
			},
			active_unit_id='u1',
			last_effect_seq=0,
		)
		prepared_draft = EffectRecordDraft(
			effect_id='effect-1',
			workflow_id='wf-1',
			run_id='run-old',
			unit_id='u1',
			effect_key='submit-application',
			attempt_id='attempt-1',
			status=EffectRecordStatus.PREPARED,
		)
		_, durable_checkpoint = storage.commit_effect_and_checkpoint(prepared_draft, prepared_checkpoint)
		executor_calls += 1
		storage.append_effect(prepared_draft.model_copy(update={'status': EffectRecordStatus.ATTEMPTED}))

		recovered = RecoveryBootstrap(storage).restore('wf-1', 'run-new')
		blocked = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id='wf-1',
			run_id='run-new',
			contract=semantic_contract,
			verifier=StaticVerifier(verification_result(semantic_contract, VerificationOutcome.VERIFIED)),
		)

		async def execute_again() -> None:
			nonlocal executor_calls
			executor_calls += 1

		with pytest.raises(SideEffectExecutionError):
			await blocked.execute(
				unit_id='u1',
				effect_key='submit-application',
				states=dict(recovered.states),
				last_effect_seq=recovered.last_effect_seq,
				executor=execute_again,
			)

		result = await reconciliation_coordinator(
			storage,
			semantic_contract,
			VerificationOutcome.VERIFIED,
		).reconcile(
			unit_id='u1',
			states=dict(recovered.states),
			last_effect_seq=durable_checkpoint.last_effect_seq,
			context={'application_exists': True},
		)

		assert executor_calls == 1
		assert result.states['u1'].status is UnitStatus.COMPLETED
		assert result.record.status is EffectRecordStatus.COMMITTED
		assert result.record.attempt_id == 'attempt-1'
		assert result.record.metadata['reconciled_from_seq'] == 2
