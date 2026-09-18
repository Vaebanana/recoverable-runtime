from datetime import datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from browser_use.recovery.contracts import UnitRuntimeState, UnitStatus
from browser_use.recovery.persistence.models import (
	EffectRecord,
	EffectRecordStatus,
	RuntimeCheckpoint,
	WorkflowRun,
)


def checkpoint(**changes: object) -> RuntimeCheckpoint:
	"""Build a valid checkpoint and selectively replace test inputs."""
	values: dict[str, object] = {
		'checkpoint_id': 'cp-1',
		'workflow_id': 'wf-1',
		'run_id': 'run-1',
		'contract_id': 'contract-1',
		'contract_version': 1,
		'unit_states': {'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE)},
		'active_unit_id': 'u1',
		'last_effect_seq': 0,
	}
	values.update(changes)
	return RuntimeCheckpoint.model_validate(values)


def effect_record(**changes: object) -> EffectRecord:
	"""Build a valid persisted effect record."""
	values: dict[str, object] = {
		'seq': 1,
		'effect_id': 'effect-1',
		'workflow_id': 'wf-1',
		'run_id': 'run-1',
		'unit_id': 'u1',
		'effect_key': 'submit-order',
		'attempt_id': 'attempt-1',
		'status': EffectRecordStatus.PREPARED,
	}
	values.update(changes)
	return EffectRecord.model_validate(values)


def test_checkpoint_rejects_a_state_key_that_does_not_match_unit_id() -> None:
	with pytest.raises(ValidationError, match='state key'):
		checkpoint(
			unit_states={'u1': UnitRuntimeState(unit_id='u2', status=UnitStatus.ACTIVE)},
		)


def test_checkpoint_active_unit_must_reference_an_in_flight_state() -> None:
	with pytest.raises(ValidationError, match='active_unit_id'):
		checkpoint(
			unit_states={'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.COMPLETED)},
		)


def test_effect_record_requires_a_positive_persisted_sequence() -> None:
	with pytest.raises(ValidationError, match='greater than or equal to 1'):
		effect_record(seq=0)


def test_persistence_models_are_frozen_and_copy_nested_mappings() -> None:
	record = effect_record(evidence={'order_id': 'order-7'}, metadata={'source': 'browser'})
	checkpoint_value = checkpoint(metadata={'reason': 'prepared'})

	with pytest.raises(TypeError):
		cast(Any, record.evidence)['order_id'] = 'order-8'
	with pytest.raises(TypeError):
		cast(Any, checkpoint_value.metadata)['reason'] = 'changed'
	with pytest.raises(ValidationError, match='frozen'):
		record.effect_key = 'changed'

	assert record.model_dump(mode='json')['evidence'] == {'order_id': 'order-7'}
	assert checkpoint_value.model_dump(mode='json')['metadata'] == {'reason': 'prepared'}


def test_workflow_run_has_a_timezone_aware_start_time() -> None:
	run = WorkflowRun(workflow_id='wf-1', run_id='run-1')

	assert isinstance(run.started_at, datetime)
	assert run.started_at.tzinfo is not None
	assert run.resumed_from_checkpoint_id is None


def test_day6_public_api_is_importable() -> None:
	from browser_use.recovery import RecoveryBootstrap, SQLiteRuntimeStorage

	assert RecoveryBootstrap is not None
	assert SQLiteRuntimeStorage is not None
