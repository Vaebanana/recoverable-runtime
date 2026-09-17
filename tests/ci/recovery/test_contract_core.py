from datetime import datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from browser_use.recovery.contracts import (
	AddUnitOp,
	ConditionSpec,
	ContractApplyResult,
	ContractDelta,
	EffectSpec,
	EffectStatus,
	Idempotency,
	ProposedUnit,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	SupersedeUnitOp,
	TargetSpec,
	UnitIdentity,
	UnitProposal,
	UnitReference,
	UnitRuntimeState,
	UnitStatus,
	UpdateConstraintsOp,
	VerificationSource,
	VerificationSpec,
	VerificationStatus,
)
from browser_use.recovery.manager import (
	ContractManager,
	ContractValidationError,
	IdentityConflictError,
	StaleDeltaError,
)
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.scheduler import RuntimeScheduler, SchedulingError


def proposal(
	intent_key: str,
	target_key: str,
	outcome_key: str,
	candidate_unit_id: str | None = None,
) -> UnitProposal:
	"""Build a complete semantic proposal for Contract Core tests."""
	return UnitProposal(
		candidate_unit_id=candidate_unit_id,
		identity=UnitIdentity(
			intent_key=intent_key,
			target_key=target_key,
			outcome_key=outcome_key,
		),
		intent=intent_key.replace('_', ' '),
		target=TargetSpec(type='job', key=target_key),
		postconditions=(ConditionSpec(description=outcome_key),),
		effect=EffectSpec(
			has_side_effect=intent_key.startswith('submit'),
			idempotency=(Idempotency.NON_IDEMPOTENT if intent_key.startswith('submit') else Idempotency.NOT_APPLICABLE),
			reversibility=(Reversibility.UNKNOWN if intent_key.startswith('submit') else Reversibility.NOT_APPLICABLE),
		),
		verification=VerificationSpec(
			required=True,
			source=VerificationSource.BROWSER,
			procedure=f'verify {outcome_key}',
		),
	)


def proposed_unit(
	temp_ref: str,
	unit_proposal: UnitProposal,
	depends_on: tuple[UnitReference, ...] = (),
) -> ProposedUnit:
	"""Build an initial proposal entry with optional temporary dependencies."""
	return ProposedUnit(temp_ref=temp_ref, proposal=unit_proposal, depends_on=depends_on)


def runtime_states(contract: SemanticContract) -> dict[str, UnitRuntimeState]:
	"""Create default runtime state without relying on the later state manager."""
	return {unit.unit_id: UnitRuntimeState(unit_id=unit.unit_id) for unit in contract.units}


def test_definition_models_are_frozen_and_forbid_unknown_fields() -> None:
	identity = UnitIdentity(
		intent_key='submit_application',
		target_key='company_a/job_1',
		outcome_key='application_submitted',
	)

	with pytest.raises(ValidationError, match='frozen'):
		identity.intent_key = 'verify_email'

	with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
		UnitIdentity.model_validate(
			{
				'intent_key': 'submit_application',
				'target_key': 'company_a/job_1',
				'outcome_key': 'application_submitted',
				'unit_id': 'llm_must_not_assign_this',
			}
		)


def test_nested_mappings_cannot_mutate_frozen_contract_data() -> None:
	target = TargetSpec(type='job', key='company_a/job_1', attributes={'company': 'company_a'})
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1)
	result = ContractApplyResult(contract=contract, superseded_units={'u1': 'u2'})

	with pytest.raises(TypeError):
		cast(Any, target.attributes)['company'] = 'company_b'
	with pytest.raises(TypeError):
		cast(Any, result.superseded_units)['u1'] = 'u3'

	assert target.model_dump()['attributes'] == {'company': 'company_a'}
	assert result.model_dump()['superseded_units'] == {'u1': 'u2'}


@pytest.mark.parametrize(
	'reference',
	[
		{},
		{'unit_id': 'u1', 'temp_ref': 'prepare'},
	],
)
def test_unit_reference_requires_exactly_one_reference(reference: dict[str, str]) -> None:
	with pytest.raises(ValidationError, match='exactly one'):
		UnitReference.model_validate(reference)


def test_identity_fingerprint_normalizes_only_case_and_outer_whitespace() -> None:
	identity = UnitIdentity(
		intent_key='  Submit_Application ',
		target_key=' Company_A/Job_1 ',
		outcome_key=' Application_Submitted  ',
	)
	assert identity.fingerprint() == (
		'submit_application',
		'company_a/job_1',
		'application_submitted',
	)


def test_proposal_rejects_identity_target_key_that_contradicts_target() -> None:
	with pytest.raises(ValidationError, match='identity target_key must match target key'):
		UnitProposal(
			identity=UnitIdentity(
				intent_key='submit_application',
				target_key='company_a/job_1',
				outcome_key='application_submitted',
			),
			intent='submit application',
			target=TargetSpec(type='job', key='company_b/job_2'),
			effect=EffectSpec(
				has_side_effect=True,
				idempotency=Idempotency.NON_IDEMPOTENT,
				reversibility=Reversibility.UNKNOWN,
			),
			verification=VerificationSpec(
				source=VerificationSource.BROWSER,
				procedure='verify application history',
			),
		)


def test_persisted_unit_rejects_identity_target_key_that_contradicts_target() -> None:
	unit_proposal = proposal('submit_application', 'company_a/job_1', 'application_submitted')
	unit_data = unit_proposal.model_dump()
	unit_data['unit_id'] = 'u1'
	unit_data.pop('candidate_unit_id')
	unit_data['target'] = {'type': 'job', 'key': 'company_b/job_2'}

	with pytest.raises(ValidationError, match='identity target_key must match target key'):
		SemanticUnit.model_validate(unit_data)


def test_contract_and_runtime_state_have_independent_defaults() -> None:
	contract = SemanticContract(contract_id='c1', task_id='task-1', version=1)
	state = UnitRuntimeState(unit_id='u1')

	assert contract.schema_version == '0.1'
	assert contract.previous_version is None
	assert contract.units == ()
	assert isinstance(contract.created_at, datetime)
	assert contract.created_at.tzinfo is not None
	assert state.status is UnitStatus.PENDING
	assert state.verification_status is VerificationStatus.NOT_CHECKED
	assert state.effect_status is EffectStatus.NOT_STARTED


def test_contract_delta_deserializes_operations_by_discriminator() -> None:
	unit_proposal = proposal('verify_email', 'account/email', 'email_verified')
	delta = ContractDelta.model_validate(
		{
			'contract_id': 'c1',
			'base_version': 1,
			'reason': 'email verification discovered',
			'operations': [
				{
					'op': 'add_unit',
					'temp_ref': 'verify_email',
					'proposal': unit_proposal.model_dump(mode='json'),
				}
			],
		}
	)

	assert len(delta.operations) == 1
	assert isinstance(delta.operations[0], AddUnitOp)
	assert delta.operations[0].temp_ref == 'verify_email'


def test_initial_contract_assigns_runtime_ids_and_resolves_forward_dependencies() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')

	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
				depends_on=(UnitReference(temp_ref='prepare'),),
			),
			proposed_unit(
				'prepare',
				proposal('prepare_application', 'company_a/job_1', 'application_ready'),
			),
		],
	)

	assert contract.contract_id == 'c1'
	assert contract.version == 1
	assert [unit.unit_id for unit in contract.units] == ['u1', 'u2']
	assert contract.get_unit('u1').depends_on == ('u2',)


def test_initial_contract_deduplicates_an_exact_normalized_fingerprint() -> None:
	ids = iter(['u1'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')

	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'first',
				proposal('prepare_application', 'company_a/job_1', 'application_ready'),
			),
			proposed_unit(
				'duplicate',
				proposal(' PREPARE_APPLICATION ', ' COMPANY_A/JOB_1 ', ' APPLICATION_READY '),
			),
		],
	)

	assert [unit.unit_id for unit in contract.units] == ['u1']


def test_explicit_candidate_unit_id_requires_matching_identity() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit('job', proposal('locate_job', 'company_a/job_1', 'job_located')),
		],
	)

	with pytest.raises(IdentityConflictError, match='does not match'):
		manager.resolve_proposal(
			contract,
			proposal('verify_email', 'account/email', 'email_verified', candidate_unit_id='u1'),
		)


def test_initial_contract_rejects_missing_dependencies() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')

	with pytest.raises(ContractValidationError, match='unknown unit_id'):
		manager.create_initial_contract(
			task_id='task-1',
			proposed_units=[
				proposed_unit(
					'submit',
					proposal('submit_application', 'company_a/job_1', 'application_submitted'),
					depends_on=(UnitReference(unit_id='missing'),),
				),
			],
		)


def test_initial_contract_rejects_dependency_cycles() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')

	with pytest.raises(ContractValidationError, match='dependency cycle'):
		manager.create_initial_contract(
			task_id='task-1',
			proposed_units=[
				proposed_unit(
					'a',
					proposal('prepare_a', 'target/a', 'a_ready'),
					depends_on=(UnitReference(temp_ref='b'),),
				),
				proposed_unit(
					'b',
					proposal('prepare_b', 'target/b', 'b_ready'),
					depends_on=(UnitReference(temp_ref='a'),),
				),
			],
		)


def test_delta_adds_a_unit_and_resolves_a_later_temp_reference_atomically() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract_v1 = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
			),
		],
	)
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='email verification discovered',
		operations=(
			AddUnitOp(
				temp_ref='verify_email',
				proposal=proposal('verify_email', 'account/email', 'email_verified'),
			),
			UpdateConstraintsOp(
				unit_id='u1',
				depends_on=(UnitReference(temp_ref='verify_email'),),
			),
		),
	)

	result = manager.apply_delta(contract_v1, runtime_states(contract_v1), delta)

	assert result.contract.version == 2
	assert result.contract.previous_version == 1
	assert result.added_unit_ids == ('u2',)
	assert result.contract.get_unit('u1').depends_on == ('u2',)
	assert contract_v1.version == 1
	assert contract_v1.get_unit('u1').depends_on == ()


def test_stale_delta_is_rejected() -> None:
	manager = ContractManager(contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(task_id='task-1', proposed_units=[])
	delta = ContractDelta(contract_id='c1', base_version=0, reason='stale')

	with pytest.raises(StaleDeltaError, match='current contract is v1'):
		manager.apply_delta(contract, runtime_states(contract), delta)


def test_delta_for_a_different_contract_is_rejected() -> None:
	manager = ContractManager(contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(task_id='task-1', proposed_units=[])
	delta = ContractDelta(contract_id='c2', base_version=1, reason='wrong task')

	with pytest.raises(ContractValidationError, match='contract_id'):
		manager.apply_delta(contract, runtime_states(contract), delta)


def test_invalid_delta_does_not_mutate_the_original_contract() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
			),
		],
	)
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='invalid dependency',
		operations=(
			AddUnitOp(
				temp_ref='verify_email',
				proposal=proposal('verify_email', 'account/email', 'email_verified'),
			),
			UpdateConstraintsOp(
				unit_id='u1',
				depends_on=(UnitReference(unit_id='does-not-exist'),),
			),
		),
	)

	with pytest.raises(ContractValidationError, match='unknown unit_id'):
		manager.apply_delta(contract, runtime_states(contract), delta)

	assert contract.version == 1
	assert [unit.unit_id for unit in contract.units] == ['u1']
	assert contract.get_unit('u1').depends_on == ()


def test_semantic_noop_delta_keeps_the_existing_contract_version() -> None:
	manager = ContractManager(contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(task_id='task-1', proposed_units=[])

	result = manager.apply_delta(
		contract,
		runtime_states(contract),
		ContractDelta(contract_id='c1', base_version=1, reason='nothing changed'),
	)

	assert result.contract is contract
	assert result.contract.version == 1


def test_canceling_constraint_updates_keep_the_existing_contract_version() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('job', proposal('locate_job', 'target/a', 'job_located'))],
	)
	original_postconditions = contract.get_unit('u1').postconditions
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='temporary constraint change was canceled',
		operations=(
			UpdateConstraintsOp(
				unit_id='u1',
				postconditions=(ConditionSpec(description='temporary meaning'),),
			),
			UpdateConstraintsOp(unit_id='u1', postconditions=original_postconditions),
		),
	)

	result = manager.apply_delta(contract, runtime_states(contract), delta)

	assert result.contract is contract
	assert result.contract.version == 1


@pytest.mark.parametrize(
	'state',
	[
		UnitRuntimeState(unit_id='u1', effect_status=EffectStatus.ATTEMPTED),
		UnitRuntimeState(unit_id='u1', status=UnitStatus.COMPLETED),
	],
)
def test_constraint_update_is_rejected_after_definition_freezes(state: UnitRuntimeState) -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
			),
		],
	)
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='unsafe rewrite',
		operations=(
			UpdateConstraintsOp(
				unit_id='u1',
				postconditions=(ConditionSpec(description='new meaning'),),
			),
		),
	)

	with pytest.raises(ContractValidationError, match='definition is frozen'):
		manager.apply_delta(contract, {'u1': state}, delta)


def test_supersede_keeps_old_unit_and_rewires_downstream_dependencies() -> None:
	ids = iter(['u1', 'u2', 'u3'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'prepare',
				proposal('prepare_application', 'company_a/job_1', 'application_ready'),
			),
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
				depends_on=(UnitReference(temp_ref='prepare'),),
			),
		],
	)
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='preparation semantics changed',
		operations=(
			SupersedeUnitOp(
				old_unit_id='u1',
				replacement=proposal(
					'prepare_application_with_email',
					'company_a/job_1',
					'application_ready_with_email',
				),
			),
		),
	)

	result = manager.apply_delta(contract, runtime_states(contract), delta)

	assert [unit.unit_id for unit in result.contract.units] == ['u1', 'u2', 'u3']
	assert result.contract.get_unit('u2').depends_on == ('u3',)
	assert result.superseded_units == {'u1': 'u3'}


def test_unit_with_unknown_effect_cannot_be_superseded() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
			),
		],
	)
	states = {'u1': UnitRuntimeState(unit_id='u1', effect_status=EffectStatus.UNKNOWN)}
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='cannot discard an unknown effect',
		operations=(
			SupersedeUnitOp(
				old_unit_id='u1',
				replacement=proposal(
					'submit_application_v2',
					'company_a/job_1',
					'application_submitted_v2',
				),
			),
		),
	)

	with pytest.raises(ContractValidationError, match='cannot be superseded'):
		manager.apply_delta(contract, states, delta)


def test_same_unit_cannot_be_superseded_twice_in_one_delta() -> None:
	ids = iter(['u1', 'u2', 'u3'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('old', proposal('prepare', 'target/a', 'ready'))],
	)
	delta = ContractDelta(
		contract_id='c1',
		base_version=1,
		reason='conflicting replacements',
		operations=(
			SupersedeUnitOp(
				old_unit_id='u1',
				replacement=proposal('prepare_v2', 'target/a', 'ready_v2'),
			),
			SupersedeUnitOp(
				old_unit_id='u1',
				replacement=proposal('prepare_v3', 'target/a', 'ready_v3'),
			),
		),
	)

	with pytest.raises(ContractValidationError, match='already superseded'):
		manager.apply_delta(contract, runtime_states(contract), delta)


def test_superseded_unit_definition_is_terminal_in_later_deltas() -> None:
	ids = iter(['u1', 'u2', 'u3'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract_v1 = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('old', proposal('prepare', 'target/a', 'ready'))],
	)
	states_v1 = RuntimeStateManager().initialize(contract_v1)
	result = manager.apply_delta(
		contract_v1,
		states_v1,
		ContractDelta(
			contract_id='c1',
			base_version=1,
			reason='replace old definition',
			operations=(
				SupersedeUnitOp(
					old_unit_id='u1',
					replacement=proposal('prepare_v2', 'target/a', 'ready_v2'),
				),
			),
		),
	)
	states_v2 = RuntimeStateManager().apply_contract_result(states_v1, result)

	with pytest.raises(ContractValidationError, match='definition is frozen'):
		manager.apply_delta(
			result.contract,
			states_v2,
			ContractDelta(
				contract_id='c1',
				base_version=2,
				reason='cannot rewrite terminal definition',
				operations=(
					UpdateConstraintsOp(
						unit_id='u1',
						postconditions=(ConditionSpec(description='changed'),),
					),
				),
			),
		)

	with pytest.raises(ContractValidationError, match='cannot be superseded'):
		manager.apply_delta(
			result.contract,
			states_v2,
			ContractDelta(
				contract_id='c1',
				base_version=2,
				reason='cannot replace terminal definition again',
				operations=(
					SupersedeUnitOp(
						old_unit_id='u1',
						replacement=proposal('prepare_v3', 'target/a', 'ready_v3'),
					),
				),
			),
		)

	with pytest.raises(ContractValidationError, match='superseded unit cannot be referenced'):
		manager.apply_delta(
			result.contract,
			states_v2,
			ContractDelta(
				contract_id='c1',
				base_version=2,
				reason='cannot bind a proposal to terminal identity',
				operations=(
					AddUnitOp(
						temp_ref='old_again',
						proposal=proposal('prepare', 'target/a', 'ready'),
					),
				),
			),
		)

	with pytest.raises(ContractValidationError, match='superseded unit cannot be referenced'):
		manager.apply_delta(
			result.contract,
			states_v2,
			ContractDelta(
				contract_id='c1',
				base_version=2,
				reason='cannot add a dependency on terminal identity',
				operations=(
					AddUnitOp(
						temp_ref='dependent',
						proposal=proposal('submit', 'target/b', 'submitted'),
						depends_on=(UnitReference(unit_id='u1'),),
					),
				),
			),
		)


def test_scheduler_returns_only_pending_units_with_completed_dependencies() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit(
				'prepare',
				proposal('prepare_application', 'company_a/job_1', 'application_ready'),
			),
			proposed_unit(
				'submit',
				proposal('submit_application', 'company_a/job_1', 'application_submitted'),
				depends_on=(UnitReference(temp_ref='prepare'),),
			),
		],
	)
	states = RuntimeStateManager().initialize(contract)
	scheduler = RuntimeScheduler()

	assert scheduler.ready_unit_ids(contract, states) == ['u1']
	states['u1'] = states['u1'].model_copy(update={'status': UnitStatus.COMPLETED})
	assert scheduler.ready_unit_ids(contract, states) == ['u2']


def test_skipped_dependency_does_not_satisfy_dependency() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit('prepare', proposal('prepare_application', 'target/a', 'application_ready')),
			proposed_unit(
				'submit',
				proposal('submit_application', 'target/a', 'application_submitted'),
				depends_on=(UnitReference(temp_ref='prepare'),),
			),
		],
	)
	states = RuntimeStateManager().initialize(contract)
	states['u1'] = states['u1'].model_copy(update={'status': UnitStatus.SKIPPED})

	assert RuntimeScheduler().ready_unit_ids(contract, states) == []


def test_scheduler_requires_runtime_state_for_every_contract_unit() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('job', proposal('locate_job', 'target/a', 'job_located'))],
	)

	with pytest.raises(SchedulingError, match='missing runtime state'):
		RuntimeScheduler().ready_unit_ids(contract, {})


@pytest.mark.parametrize(
	'states, message',
	[
		(
			{
				'u1': UnitRuntimeState(unit_id='u1'),
				'ghost': UnitRuntimeState(unit_id='ghost'),
			},
			'unexpected runtime state',
		),
		({'u1': UnitRuntimeState(unit_id='ghost')}, 'runtime state key does not match'),
	],
)
def test_contract_manager_rejects_malformed_runtime_state_maps(
	states: dict[str, UnitRuntimeState],
	message: str,
) -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('job', proposal('locate_job', 'target/a', 'job_located'))],
	)

	with pytest.raises(ContractValidationError, match=message):
		manager.apply_delta(
			contract,
			states,
			ContractDelta(contract_id='c1', base_version=1, reason='validate states'),
		)


def test_scheduler_rejects_state_key_identity_mismatch() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('job', proposal('locate_job', 'target/a', 'job_located'))],
	)

	with pytest.raises(SchedulingError, match='runtime state key does not match'):
		RuntimeScheduler().ready_unit_ids(contract, {'u1': UnitRuntimeState(unit_id='ghost')})


def test_scheduler_rejects_activation_of_extra_ghost_state() -> None:
	manager = ContractManager(unit_id_factory=lambda: 'u1', contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[proposed_unit('job', proposal('locate_job', 'target/a', 'job_located'))],
	)
	states = {
		'u1': UnitRuntimeState(unit_id='u1'),
		'ghost': UnitRuntimeState(unit_id='ghost', status=UnitStatus.ACTIVE),
	}

	with pytest.raises(SchedulingError, match='unexpected runtime state'):
		RuntimeScheduler().validate_activation(contract, states, 'ghost')


def test_runtime_allows_only_one_active_unit_without_changing_contract_version() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit('a', proposal('locate_job', 'target/a', 'job_a_located')),
			proposed_unit('b', proposal('locate_job', 'target/b', 'job_b_located')),
		],
	)
	state_manager = RuntimeStateManager()
	states = state_manager.activate(contract, state_manager.initialize(contract), 'u1')

	assert states['u1'].status is UnitStatus.ACTIVE
	assert contract.version == 1
	with pytest.raises(SchedulingError, match='another unit is already active'):
		state_manager.activate(contract, states, 'u2')


def test_contract_result_initializes_new_state_and_marks_old_unit_superseded() -> None:
	ids = iter(['u1', 'u2'])
	manager = ContractManager(unit_id_factory=lambda: next(ids), contract_id_factory=lambda: 'c1')
	contract = manager.create_initial_contract(
		task_id='task-1',
		proposed_units=[
			proposed_unit('old', proposal('prepare_application', 'target/a', 'application_ready')),
		],
	)
	states = RuntimeStateManager().initialize(contract)
	result = manager.apply_delta(
		contract,
		states,
		ContractDelta(
			contract_id='c1',
			base_version=1,
			reason='replace definition',
			operations=(
				SupersedeUnitOp(
					old_unit_id='u1',
					replacement=proposal('prepare_v2', 'target/a', 'application_ready_v2'),
				),
			),
		),
	)

	updated = RuntimeStateManager().apply_contract_result(states, result)

	assert updated['u1'].status is UnitStatus.SUPERSEDED
	assert updated['u1'].superseded_by == 'u2'
	assert updated['u2'].status is UnitStatus.PENDING
	assert states['u1'].status is UnitStatus.PENDING
	assert RuntimeScheduler().ready_unit_ids(result.contract, updated) == ['u2']
