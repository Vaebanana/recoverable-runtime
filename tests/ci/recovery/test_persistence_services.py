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
from browser_use.recovery.persistence.checkpoint import CheckpointError, CheckpointManager
from browser_use.recovery.persistence.effect_ledger import EffectLedger
from browser_use.recovery.persistence.models import EffectRecordStatus, WorkflowRun
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.scheduler import RuntimeScheduler


def semantic_unit(unit_id: str) -> SemanticUnit:
	"""Build one complete side-effect unit for persistence tests."""
	return SemanticUnit(
		unit_id=unit_id,
		identity=UnitIdentity(intent_key=f'submit-{unit_id}', target_key=f'order/{unit_id}', outcome_key='submitted'),
		intent='submit order',
		target=TargetSpec(type='order', key=f'order/{unit_id}'),
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


def initialize_storage(storage: SQLiteRuntimeStorage, contract: SemanticContract) -> None:
	"""Persist the run and Contract required by checkpoint foreign keys."""
	storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
	storage.save_contract('wf-1', contract)


def test_checkpoint_manager_round_trips_stable_state_and_readiness_is_recomputed(tmp_path: Path) -> None:
	contract = SemanticContract(contract_id='contract-1', task_id='task-1', version=1, units=(semantic_unit('u1'),))
	states = {'u1': UnitRuntimeState(unit_id='u1', effect_status=EffectStatus.NOT_STARTED)}
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_storage(storage, contract)
		manager = CheckpointManager(storage, checkpoint_id_factory=lambda: 'cp-1')

		saved = manager.save(
			workflow_id='wf-1',
			run_id='run-1',
			contract=contract,
			states=states,
			last_effect_seq=0,
			metadata={'reason': 'initial'},
		)
		loaded = manager.load_latest('wf-1')

		assert loaded == saved
		assert loaded is not None
		assert loaded.active_unit_id is None
		assert RuntimeScheduler().ready_unit_ids(contract, dict(loaded.unit_states)) == ['u1']


def test_checkpoint_manager_rejects_multiple_in_flight_units(tmp_path: Path) -> None:
	contract = SemanticContract(
		contract_id='contract-1',
		task_id='task-1',
		version=1,
		units=(semantic_unit('u1'), semantic_unit('u2')),
	)
	states = {
		'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE),
		'u2': UnitRuntimeState(unit_id='u2', status=UnitStatus.VERIFYING),
	}
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_storage(storage, contract)
		manager = CheckpointManager(storage)

		with pytest.raises(CheckpointError, match='multiple in-flight'):
			manager.build(
				workflow_id='wf-1',
				run_id='run-1',
				contract=contract,
				states=states,
				last_effect_seq=0,
			)


def test_effect_ledger_appends_named_states_without_mutating_history(tmp_path: Path) -> None:
	contract = SemanticContract(contract_id='contract-1', task_id='task-1', version=1)
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_storage(storage, contract)
		ledger = EffectLedger(storage)

		prepared = ledger.append_prepared(
			effect_id='effect-1',
			workflow_id='wf-1',
			run_id='run-1',
			unit_id='u1',
			effect_key='submit-order',
			attempt_id='attempt-1',
		)
		committed = ledger.append_committed(
			effect_id='effect-1',
			workflow_id='wf-1',
			run_id='run-1',
			unit_id='u1',
			effect_key='submit-order',
			attempt_id='attempt-1',
			evidence={'order_id': 'order-7'},
		)

		assert prepared.status is EffectRecordStatus.PREPARED
		assert committed.status is EffectRecordStatus.COMMITTED
		assert [record.status for record in ledger.records_for_unit('wf-1', 'u1')] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.COMMITTED,
		]
		assert ledger.latest_for_unit('wf-1', 'u1') == committed
