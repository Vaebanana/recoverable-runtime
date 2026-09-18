from pathlib import Path

import pytest

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
from browser_use.recovery.persistence.models import EffectRecordStatus, WorkflowRun
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.side_effects import SideEffectCoordinator
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)


def side_effect_contract() -> SemanticContract:
	"""Build one active unit with externally verifiable effects."""
	unit = SemanticUnit(
		unit_id='u1',
		identity=UnitIdentity(intent_key='submit-order', target_key='order/7', outcome_key='submitted'),
		intent='submit order',
		target=TargetSpec(type='order', key='order/7'),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
		),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='verify order history',
		),
	)
	return SemanticContract(contract_id='contract-1', task_id='task-1', version=1, units=(unit,))


def active_states() -> dict[str, UnitRuntimeState]:
	"""Return the pre-effect state snapshot."""
	return {
		'u1': UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.ACTIVE,
			effect_status=EffectStatus.NOT_STARTED,
		)
	}


class StaticVerifier:
	"""Return one deterministic observational result to the real verification manager."""

	observational = True

	def __init__(self, contract: SemanticContract, outcome: VerificationOutcome) -> None:
		self._contract = contract
		self._outcome = outcome

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		observed = {'target.key': unit.target.key} if self._outcome is VerificationOutcome.VERIFIED else {}
		return VerificationResult(
			outcome=self._outcome,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(self._contract.get_unit(unit.unit_id)),
				observed=observed,
				source=VerificationSource.BROWSER,
				summary='deterministic test evidence',
			),
		)


def coordinator(
	storage: SQLiteRuntimeStorage,
	contract: SemanticContract,
	verifier: StaticVerifier,
) -> SideEffectCoordinator:
	"""Build a deterministic coordinator for one workflow run."""
	checkpoint_ids = iter(['cp-prepared', 'cp-final'])
	return SideEffectCoordinator(
		storage=storage,
		checkpoint_manager=CheckpointManager(storage, checkpoint_id_factory=lambda: next(checkpoint_ids)),
		workflow_id='wf-1',
		run_id='run-1',
		contract=contract,
		verifier=verifier,
		effect_id_factory=lambda: 'effect-1',
		attempt_id_factory=lambda: 'attempt-1',
	)


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'outcome, expected_status, expected_effect, expected_record',
	[
		(
			VerificationOutcome.VERIFIED,
			UnitStatus.COMPLETED,
			EffectStatus.COMMITTED,
			EffectRecordStatus.COMMITTED,
		),
		(
			VerificationOutcome.REJECTED,
			UnitStatus.ACTIVE,
			EffectStatus.NOT_APPLIED,
			EffectRecordStatus.NOT_APPLIED,
		),
		(
			VerificationOutcome.INCONCLUSIVE,
			UnitStatus.UNKNOWN,
			EffectStatus.UNKNOWN,
			EffectRecordStatus.UNKNOWN,
		),
	],
)
async def test_verification_outcome_is_atomically_reflected_in_ledger_and_checkpoint(
	tmp_path: Path,
	outcome: VerificationOutcome,
	expected_status: UnitStatus,
	expected_effect: EffectStatus,
	expected_record: EffectRecordStatus,
) -> None:
	contract = side_effect_contract()
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		async def execute_action() -> dict[str, str]:
			return {'action': 'clicked'}

		result = await coordinator(storage, contract, StaticVerifier(contract, outcome)).execute(
			unit_id='u1',
			effect_key='submit-order',
			states=active_states(),
			last_effect_seq=0,
			executor=execute_action,
		)

		assert result.states['u1'].status is expected_status
		assert result.states['u1'].effect_status is expected_effect
		assert result.records[-1].status is expected_record
		assert [record.status for record in storage.read_effects('wf-1')] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			expected_record,
		]
		checkpoint = storage.load_latest_checkpoint('wf-1')
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == result.records[-1].seq
		assert checkpoint.unit_states['u1'] == result.states['u1']


@pytest.mark.asyncio
async def test_action_success_alone_never_marks_effect_committed(tmp_path: Path) -> None:
	contract = side_effect_contract()
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		async def successful_action() -> str:
			return 'browser action succeeded'

		result = await coordinator(
			storage,
			contract,
			StaticVerifier(contract, VerificationOutcome.INCONCLUSIVE),
		).execute(
			unit_id='u1',
			effect_key='submit-order',
			states=active_states(),
			last_effect_seq=0,
			executor=successful_action,
		)

		assert result.action_succeeded is True
		assert result.records[-1].status is EffectRecordStatus.UNKNOWN
		assert result.states['u1'].status is UnitStatus.UNKNOWN


@pytest.mark.asyncio
async def test_action_exception_is_persisted_as_unknown_without_retry(tmp_path: Path) -> None:
	contract = side_effect_contract()
	attempts = 0
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		async def failing_action() -> None:
			nonlocal attempts
			attempts += 1
			raise RuntimeError('connection dropped after click')

		result = await coordinator(
			storage,
			contract,
			StaticVerifier(contract, VerificationOutcome.VERIFIED),
		).execute(
			unit_id='u1',
			effect_key='submit-order',
			states=active_states(),
			last_effect_seq=0,
			executor=failing_action,
		)

		assert attempts == 1
		assert result.action_succeeded is False
		assert result.action_error == 'RuntimeError: connection dropped after click'
		assert result.verification_result.outcome is VerificationOutcome.INCONCLUSIVE
		assert result.states['u1'].status is UnitStatus.UNKNOWN
		assert result.states['u1'].verification_status is VerificationStatus.INCONCLUSIVE
		assert result.records[-1].status is EffectRecordStatus.UNKNOWN
