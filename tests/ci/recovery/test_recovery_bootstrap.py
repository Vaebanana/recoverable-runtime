from pathlib import Path

import pytest

from browser_use.recovery.bootstrap import RecoveredRuntime, RecoveryBootstrap, normalize_after_restart
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
from browser_use.recovery.persistence.models import (
	EffectRecord,
	EffectRecordDraft,
	EffectRecordStatus,
	RuntimeCheckpoint,
	WorkflowRun,
)
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.scheduler import RuntimeScheduler


def unit(*, has_side_effect: bool) -> SemanticUnit:
	"""Build one unit with explicit effect semantics."""
	return SemanticUnit(
		unit_id='u1',
		identity=UnitIdentity(intent_key='work', target_key='target/1', outcome_key='done'),
		intent='perform work',
		target=TargetSpec(type='target', key='target/1'),
		effect=EffectSpec(
			has_side_effect=has_side_effect,
			idempotency=Idempotency.NON_IDEMPOTENT if has_side_effect else Idempotency.NOT_APPLICABLE,
			reversibility=Reversibility.UNKNOWN if has_side_effect else Reversibility.NOT_APPLICABLE,
		),
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='verify result'),
	)


def contract(*, has_side_effect: bool, version: int = 1) -> SemanticContract:
	"""Build one versioned Contract for restart tests."""
	return SemanticContract(
		contract_id='contract-1',
		task_id='task-1',
		version=version,
		previous_version=version - 1 if version > 1 else None,
		units=(unit(has_side_effect=has_side_effect),),
	)


def ledger_record(status: EffectRecordStatus) -> EffectRecord:
	"""Build one persisted ledger fact for normalization tests."""
	return EffectRecord(
		seq=1,
		effect_id='effect-1',
		workflow_id='wf-1',
		run_id='run-old',
		unit_id='u1',
		effect_key='submit',
		attempt_id='attempt-1',
		status=status,
	)


@pytest.mark.parametrize(
	'has_side_effect, state, latest_status, expected_status, expected_effect, expected_verification',
	[
		(
			True,
			UnitRuntimeState(unit_id='u1', status=UnitStatus.COMPLETED, effect_status=EffectStatus.COMMITTED),
			EffectRecordStatus.COMMITTED,
			UnitStatus.COMPLETED,
			EffectStatus.COMMITTED,
			VerificationStatus.NOT_CHECKED,
		),
		(
			True,
			UnitRuntimeState(unit_id='u1'),
			None,
			UnitStatus.PENDING,
			EffectStatus.NOT_STARTED,
			VerificationStatus.NOT_CHECKED,
		),
		(
			False,
			UnitRuntimeState(
				unit_id='u1',
				status=UnitStatus.ACTIVE,
				effect_status=EffectStatus.NOT_APPLICABLE,
			),
			None,
			UnitStatus.ACTIVE,
			EffectStatus.NOT_APPLICABLE,
			VerificationStatus.NOT_CHECKED,
		),
		(
			True,
			UnitRuntimeState(
				unit_id='u1',
				status=UnitStatus.ACTIVE,
				effect_status=EffectStatus.NOT_APPLIED,
			),
			EffectRecordStatus.NOT_APPLIED,
			UnitStatus.ACTIVE,
			EffectStatus.NOT_APPLIED,
			VerificationStatus.NOT_CHECKED,
		),
		(
			True,
			UnitRuntimeState(
				unit_id='u1',
				status=UnitStatus.ACTIVE,
				effect_status=EffectStatus.COMMITTED,
			),
			EffectRecordStatus.COMMITTED,
			UnitStatus.COMPLETION_CANDIDATE,
			EffectStatus.COMMITTED,
			VerificationStatus.NOT_CHECKED,
		),
		(
			False,
			UnitRuntimeState(
				unit_id='u1',
				status=UnitStatus.VERIFYING,
				verification_status=VerificationStatus.CHECKING,
				effect_status=EffectStatus.NOT_APPLICABLE,
			),
			None,
			UnitStatus.COMPLETION_CANDIDATE,
			EffectStatus.NOT_APPLICABLE,
			VerificationStatus.NOT_CHECKED,
		),
	],
)
def test_normalize_after_restart_preserves_safe_states_and_requests_reverification(
	has_side_effect: bool,
	state: UnitRuntimeState,
	latest_status: EffectRecordStatus | None,
	expected_status: UnitStatus,
	expected_effect: EffectStatus,
	expected_verification: VerificationStatus,
) -> None:
	records = () if latest_status is None else (ledger_record(latest_status),)

	normalized = normalize_after_restart(contract(has_side_effect=has_side_effect), {'u1': state}, records)

	assert normalized['u1'].status is expected_status
	assert normalized['u1'].effect_status is expected_effect
	assert normalized['u1'].verification_status is expected_verification


@pytest.mark.parametrize(
	'latest_status',
	[EffectRecordStatus.PREPARED, EffectRecordStatus.ATTEMPTED, EffectRecordStatus.UNKNOWN],
)
def test_interrupted_effect_becomes_unknown(latest_status: EffectRecordStatus) -> None:
	state = UnitRuntimeState(
		unit_id='u1',
		status=UnitStatus.ACTIVE,
		effect_status=EffectStatus.ATTEMPTED,
	)

	normalized = normalize_after_restart(
		contract(has_side_effect=True),
		{'u1': state},
		(ledger_record(latest_status),),
	)

	assert normalized['u1'].status is UnitStatus.UNKNOWN
	assert normalized['u1'].effect_status is EffectStatus.UNKNOWN
	assert normalized['u1'].verification_status is VerificationStatus.INCONCLUSIVE


def test_restore_loads_checkpoint_contract_version_and_starts_a_new_run(tmp_path: Path) -> None:
	contract_v1 = contract(has_side_effect=False, version=1)
	contract_v2 = contract(has_side_effect=False, version=2)
	checkpoint = RuntimeCheckpoint(
		checkpoint_id='cp-1',
		workflow_id='wf-1',
		run_id='run-old',
		contract_id='contract-1',
		contract_version=1,
		unit_states={'u1': UnitRuntimeState(unit_id='u1', effect_status=EffectStatus.NOT_APPLICABLE)},
		last_effect_seq=0,
	)
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.save_contract('wf-1', contract_v1)
		storage.save_contract('wf-1', contract_v2)
		storage.save_checkpoint(checkpoint)

		recovered = RecoveryBootstrap(storage).restore('wf-1', 'run-new')

		assert isinstance(recovered, RecoveredRuntime)
		assert recovered.workflow_id == 'wf-1'
		assert recovered.run_id == 'run-new'
		assert recovered.resumed_from_checkpoint_id == 'cp-1'
		assert recovered.contract.version == 1
		assert RuntimeScheduler().ready_unit_ids(recovered.contract, dict(recovered.states)) == ['u1']


def test_restore_uses_checkpoint_boundary_instead_of_uncommitted_ledger_tail(tmp_path: Path) -> None:
	semantic_contract = contract(has_side_effect=True)
	states = {
		'u1': UnitRuntimeState(
			unit_id='u1',
			status=UnitStatus.ACTIVE,
			effect_status=EffectStatus.NOT_STARTED,
		)
	}
	checkpoint = RuntimeCheckpoint(
		checkpoint_id='cp-prepared',
		workflow_id='wf-1',
		run_id='run-old',
		contract_id='contract-1',
		contract_version=1,
		unit_states=states,
		active_unit_id='u1',
		last_effect_seq=0,
	)
	prepared_draft = EffectRecordDraft(
		effect_id='effect-1',
		workflow_id='wf-1',
		run_id='run-old',
		unit_id='u1',
		effect_key='submit',
		attempt_id='attempt-1',
		status=EffectRecordStatus.PREPARED,
	)
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.save_contract('wf-1', semantic_contract)
		_, committed_checkpoint = storage.commit_effect_and_checkpoint(prepared_draft, checkpoint)
		storage.append_effect(prepared_draft.model_copy(update={'status': EffectRecordStatus.COMMITTED}))

		recovered = RecoveryBootstrap(storage).restore('wf-1', 'run-new')

		assert committed_checkpoint.last_effect_seq == 1
		assert recovered.last_effect_seq == 1
		assert recovered.states['u1'].status is UnitStatus.UNKNOWN
		assert recovered.states['u1'].effect_status is EffectStatus.UNKNOWN


def test_restore_sees_attempted_fact_after_prepared_checkpoint(tmp_path: Path) -> None:
	semantic_contract = contract(has_side_effect=True)
	checkpoint = RuntimeCheckpoint(
		checkpoint_id='cp-prepared',
		workflow_id='wf-1',
		run_id='run-old',
		contract_id='contract-1',
		contract_version=1,
		unit_states={'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE)},
		active_unit_id='u1',
		last_effect_seq=0,
	)
	draft = EffectRecordDraft(
		effect_id='effect-1',
		workflow_id='wf-1',
		run_id='run-old',
		unit_id='u1',
		effect_key='submit',
		attempt_id='attempt-1',
		status=EffectRecordStatus.PREPARED,
	)
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-old'))
		storage.save_contract('wf-1', semantic_contract)
		storage.commit_effect_and_checkpoint(draft, checkpoint)
		storage.append_effect(draft.model_copy(update={'status': EffectRecordStatus.ATTEMPTED}))
		recovered = RecoveryBootstrap(storage).restore('wf-1', 'run-new')
		assert recovered.last_effect_seq == 2
		assert recovered.states['u1'].status is UnitStatus.UNKNOWN
