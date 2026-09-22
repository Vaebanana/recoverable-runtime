"""Day 9 fault-injection seams around the durable PREPARED boundary."""

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
)
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.recovery.persistence.models import EffectRecordStatus
from browser_use.recovery.side_effects import SideEffectCoordinator, SimulatedCrash
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)


def contract() -> SemanticContract:
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
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='check status'),
	)
	return SemanticContract(contract_id='contract-1', task_id='task-1', version=1, units=(unit,))


class VerifiedVerifier:
	observational = True

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		return VerificationResult(
			outcome=VerificationOutcome.VERIFIED,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed={'target.key': unit.target.key},
				source=unit.verification.source,
				summary='verified',
			),
		)


@pytest.mark.asyncio
async def test_crash_after_prepared_persists_only_prepared_and_never_calls_executor(tmp_path: Path) -> None:
	semantic_contract = contract()
	executor_calls = 0
	hook_calls = 0
	states = {
		'u1': UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.ACTIVE,
			effect_status=EffectStatus.NOT_STARTED,
		)
	}

	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', semantic_contract)
		coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id='wf-1',
			run_id='run-1',
			contract=semantic_contract,
			verifier=VerifiedVerifier(),
			effect_id_factory=lambda: 'effect-1',
			attempt_id_factory=lambda: 'attempt-1',
		)

		def crash_here() -> None:
			nonlocal hook_calls
			hook_calls += 1
			raise SimulatedCrash('crash after PREPARED')

		async def executor() -> None:
			nonlocal executor_calls
			executor_calls += 1

		with pytest.raises(SimulatedCrash):
			await coordinator.execute(
				unit_id='u1',
				effect_key='submit-application',
				states=states,
				last_effect_seq=0,
				executor=executor,
				after_prepared_hook=crash_here,
			)

		assert hook_calls == 1
		assert executor_calls == 0
		records = storage.read_effects('wf-1')
		assert [record.status for record in records] == [EffectRecordStatus.PREPARED]
		assert records[0].effect_id == 'effect-1'
		assert records[0].attempt_id == 'attempt-1'
		checkpoint = storage.load_latest_checkpoint('wf-1')
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == records[0].seq
		assert checkpoint.metadata['effect_phase'] == EffectRecordStatus.PREPARED.value
