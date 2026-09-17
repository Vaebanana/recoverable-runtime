import pytest

from browser_use.recovery.contracts import (
	ConditionSpec,
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
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.semantic_context import SemanticContextBuilder, SemanticContextError


def make_unit(
	unit_id: str,
	target_key: str,
	*,
	depends_on: tuple[str, ...] = (),
	has_side_effect: bool = False,
) -> SemanticUnit:
	"""Build one unit for semantic-context tests."""
	return SemanticUnit(
		unit_id=unit_id,
		identity=UnitIdentity(
			intent_key='submit_application' if has_side_effect else 'locate_job',
			target_key=target_key,
			outcome_key='application_submitted' if has_side_effect else 'job_located',
		),
		intent='submit application' if has_side_effect else 'locate job',
		target=TargetSpec(type='job', key=target_key, attributes={'job_id': target_key.rsplit('/', 1)[-1]}),
		depends_on=depends_on,
		postconditions=(ConditionSpec(description='target outcome exists'),),
		effect=EffectSpec(
			has_side_effect=has_side_effect,
			idempotency=Idempotency.NON_IDEMPOTENT if has_side_effect else Idempotency.NOT_APPLICABLE,
			reversibility=Reversibility.UNKNOWN if has_side_effect else Reversibility.NOT_APPLICABLE,
			description='creates an external application' if has_side_effect else None,
		),
		verification=VerificationSpec(
			required=has_side_effect,
			source=VerificationSource.BROWSER,
			procedure='inspect the target record',
		),
	)


def test_context_contains_active_unit_semantics_and_deterministic_rendering() -> None:
	unit = make_unit('u1', 'company_a/job_123', has_side_effect=True)
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=3, units=(unit,))
	state_manager = RuntimeStateManager()
	states = state_manager.activate(contract, state_manager.initialize(contract), 'u1')

	context = SemanticContextBuilder().build(contract, states)

	assert context.contract_id == 'c1'
	assert context.contract_version == 3
	assert context.active_unit is not None
	assert context.active_unit.unit_id == 'u1'
	assert context.active_unit.target_key == 'company_a/job_123'
	assert context.active_unit.target_attributes == {'job_id': 'job_123'}
	assert context.active_unit.postconditions == ('target outcome exists',)
	assert context.active_unit.verification_procedure == 'inspect the target record'
	assert context.active_unit.effect_warning == 'non-idempotent side effect'
	assert context.ready_unit_ids == ()
	assert context.render() == (
		'Semantic runtime context\n'
		'Contract: c1 v3\n'
		'Current unit: u1 — submit application\n'
		'Target: job company_a/job_123 (job_id=job_123)\n'
		'Expected postconditions:\n'
		'- target outcome exists\n'
		'Verification: required; source=browser; procedure=inspect the target record\n'
		'Effect: side_effect=true; idempotency=non_idempotent; reversibility=unknown\n'
		'Warning: non-idempotent side effect\n'
		'Ready units: none\n'
		'Progress blocked: no'
	)


def test_context_derives_ready_units_without_persisting_ready_status() -> None:
	u1 = make_unit('u1', 'company_a/job_1')
	u2 = make_unit('u2', 'company_a/job_2', depends_on=('u1',))
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1, units=(u1, u2))
	states = RuntimeStateManager().initialize(contract)

	context = SemanticContextBuilder().build(contract, states)

	assert context.active_unit is None
	assert context.ready_unit_ids == ('u1',)
	assert states['u1'].status is UnitStatus.PENDING


def test_unknown_state_blocks_progress_and_identifies_affected_units() -> None:
	u1 = make_unit('u1', 'company_a/job_1', has_side_effect=True)
	u2 = make_unit('u2', 'company_a/job_2')
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1, units=(u1, u2))
	states = RuntimeStateManager().initialize(contract)
	states['u1'] = states['u1'].model_copy(
		update={
			'status': UnitStatus.UNKNOWN,
			'effect_status': EffectStatus.UNKNOWN,
		}
	)

	context = SemanticContextBuilder().build(contract, states)

	assert context.progress_blocked is True
	assert context.blocked_unit_ids == ('u1',)
	assert context.ready_unit_ids == ('u2',)
	assert context.block_reason == 'UNKNOWN unit state requires reconciliation before normal progress'
	assert 'Blocked units: u1' in context.render()


def test_context_rejects_multiple_active_units() -> None:
	u1 = make_unit('u1', 'company_a/job_1')
	u2 = make_unit('u2', 'company_a/job_2')
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1, units=(u1, u2))
	states = {
		'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE),
		'u2': UnitRuntimeState(unit_id='u2', status=UnitStatus.ACTIVE),
	}

	with pytest.raises(SemanticContextError, match='multiple ACTIVE'):
		SemanticContextBuilder().build(contract, states)


def test_context_requires_exact_runtime_state_coverage() -> None:
	u1 = make_unit('u1', 'company_a/job_1')
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1, units=(u1,))

	with pytest.raises(ValueError, match='missing runtime state'):
		SemanticContextBuilder().build(contract, {})
