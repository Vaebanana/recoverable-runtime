import sqlite3
from pathlib import Path

import pytest

from browser_use.recovery.contracts import (
	EffectStatus,
	SemanticContract,
	UnitRuntimeState,
	UnitStatus,
)
from browser_use.recovery.persistence.models import (
	EffectRecordDraft,
	EffectRecordStatus,
	RuntimeCheckpoint,
	WorkflowRun,
)
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage, StorageConflictError


def initialize_workflow(storage: SQLiteRuntimeStorage) -> SemanticContract:
	"""Create one run and Contract version used by storage tests."""
	contract = SemanticContract(contract_id='contract-1', task_id='task-1', version=1)
	storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
	storage.save_contract('wf-1', contract)
	return contract


def effect_draft(status: EffectRecordStatus, *, effect_id: str) -> EffectRecordDraft:
	"""Build one append-only effect fact."""
	return EffectRecordDraft(
		effect_id=effect_id,
		workflow_id='wf-1',
		run_id='run-1',
		unit_id='u1',
		effect_key='submit-order',
		attempt_id='attempt-1',
		status=status,
		evidence={'order_id': 'order-7'},
	)


def runtime_checkpoint(*, contract_version: int = 1, last_effect_seq: int = 0) -> RuntimeCheckpoint:
	"""Build a valid checkpoint for one active side-effect unit."""
	return RuntimeCheckpoint(
		checkpoint_id=f'cp-{contract_version}-{last_effect_seq}',
		workflow_id='wf-1',
		run_id='run-1',
		contract_id='contract-1',
		contract_version=contract_version,
		unit_states={
			'u1': UnitRuntimeState(
				unit_id='u1',
				status=UnitStatus.ACTIVE,
				effect_status=EffectStatus.ATTEMPTED,
			)
		},
		active_unit_id='u1',
		last_effect_seq=last_effect_seq,
		metadata={'reason': 'test'},
	)


def test_contract_versions_are_immutable_and_round_trip(tmp_path: Path) -> None:
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		contract = initialize_workflow(storage)

		assert storage.load_contract('wf-1', 'contract-1', 1) == contract
		storage.save_contract('wf-1', contract)

		conflicting = contract.model_copy(update={'task_id': 'different-task'})
		with pytest.raises(StorageConflictError, match='immutable'):
			storage.save_contract('wf-1', conflicting)


def test_effect_sequence_is_monotonic_and_database_rejects_history_mutation(tmp_path: Path) -> None:
	database_path = tmp_path / 'runtime.db'
	with SQLiteRuntimeStorage(database_path) as storage:
		initialize_workflow(storage)
		prepared = storage.append_effect(effect_draft(EffectRecordStatus.PREPARED, effect_id='effect-1'))
		attempted = storage.append_effect(effect_draft(EffectRecordStatus.ATTEMPTED, effect_id='effect-2'))

		assert (prepared.seq, attempted.seq) == (1, 2)
		assert [record.status for record in storage.read_effects('wf-1')] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
		]

	with sqlite3.connect(database_path) as connection:
		with pytest.raises(sqlite3.IntegrityError, match='append-only'):
			connection.execute("UPDATE effect_ledger SET status = 'committed' WHERE seq = 1")
		with pytest.raises(sqlite3.IntegrityError, match='append-only'):
			connection.execute('DELETE FROM effect_ledger WHERE seq = 1')


def test_latest_checkpoint_round_trips_nested_unit_states(tmp_path: Path) -> None:
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_workflow(storage)
		checkpoint = runtime_checkpoint()

		storage.save_checkpoint(checkpoint)

		loaded = storage.load_latest_checkpoint('wf-1')
		assert loaded == checkpoint
		assert loaded is not None
		assert loaded.unit_states['u1'].effect_status is EffectStatus.ATTEMPTED
		assert loaded.metadata == {'reason': 'test'}


def test_effect_and_checkpoint_rollback_together_when_checkpoint_insert_fails(tmp_path: Path) -> None:
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_workflow(storage)
		invalid_checkpoint = runtime_checkpoint(contract_version=2)

		with pytest.raises(StorageConflictError, match='atomic effect/checkpoint'):
			storage.commit_effect_and_checkpoint(
				effect_draft(EffectRecordStatus.COMMITTED, effect_id='effect-1'),
				invalid_checkpoint,
			)

		assert storage.read_effects('wf-1') == ()
		assert storage.load_latest_checkpoint('wf-1') is None


def test_atomic_commit_tracks_the_inserted_effect_sequence_in_checkpoint(tmp_path: Path) -> None:
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		initialize_workflow(storage)

		record, checkpoint = storage.commit_effect_and_checkpoint(
			effect_draft(EffectRecordStatus.PREPARED, effect_id='effect-1'),
			runtime_checkpoint(),
		)

		assert record.seq == 1
		assert checkpoint.last_effect_seq == record.seq
		assert storage.load_latest_checkpoint('wf-1') == checkpoint
