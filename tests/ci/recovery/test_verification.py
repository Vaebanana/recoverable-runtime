import asyncio
from collections.abc import Mapping
from typing import cast

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
	VerificationStatus,
)
from browser_use.recovery.runtime_state import RuntimeStateManager, RuntimeStateTransitionError
from browser_use.recovery.verification import (
	VerificationError,
	VerificationEvidence,
	VerificationManager,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)


def make_unit(unit_id: str = 'u1', *, has_side_effect: bool = True) -> SemanticUnit:
	"""Build a semantic unit with stable verification identity."""
	return SemanticUnit(
		unit_id=unit_id,
		identity=UnitIdentity(
			intent_key='submit_application' if has_side_effect else 'locate_application',
			target_key='company_a/job_123',
			outcome_key='application_submitted' if has_side_effect else 'application_located',
		),
		intent='submit application' if has_side_effect else 'locate application',
		target=TargetSpec(
			type='job',
			key='company_a/job_123',
			attributes={'job_id': 'job_123', 'company': 'Company A'},
		),
		postconditions=(ConditionSpec(description='application exists with submitted status'),),
		effect=EffectSpec(
			has_side_effect=has_side_effect,
			idempotency=Idempotency.NON_IDEMPOTENT if has_side_effect else Idempotency.NOT_APPLICABLE,
			reversibility=Reversibility.UNKNOWN if has_side_effect else Reversibility.NOT_APPLICABLE,
		),
		verification=VerificationSpec(
			required=has_side_effect,
			source=VerificationSource.BROWSER,
			procedure='inspect application history',
		),
	)


def make_contract(*units: SemanticUnit) -> SemanticContract:
	"""Build a contract containing the supplied units."""
	return SemanticContract(contract_id='c1', task_id='task-1', version=1, units=units)


def make_result(unit: SemanticUnit, outcome: VerificationOutcome) -> VerificationResult:
	"""Build a verification result whose expected evidence comes from Contract."""
	return VerificationResult(
		outcome=outcome,
		evidence=VerificationEvidence(
			expected=expected_target_evidence(unit),
			observed={'job_id': 'job_123', 'company': 'Company A', 'status': 'submitted'},
			source=VerificationSource.BROWSER,
			summary='application history was inspected',
		),
		detail=f'outcome={outcome.value}',
	)


class StaticVerifier:
	"""Return one deterministic result for state-machine tests."""

	observational = True

	def __init__(self, result: VerificationResult) -> None:
		self.result = result

	async def verify(self, unit: SemanticUnit, context: str) -> VerificationResult:
		assert context == 'browser snapshot'
		return self.result


class MutatingVerifier(StaticVerifier):
	"""Declare a verifier that violates the observational contract."""

	observational = False


class FailingVerifier:
	"""Simulate a verifier whose evidence source is temporarily unavailable."""

	observational = True

	async def verify(self, unit: SemanticUnit, context: str) -> VerificationResult:
		raise TimeoutError('application history timed out')


class CancelledVerifier:
	"""Simulate caller cancellation, which must not be converted to evidence."""

	observational = True

	async def verify(self, unit: SemanticUnit, context: str) -> VerificationResult:
		raise asyncio.CancelledError


def candidate_states(
	contract: SemanticContract,
	state_manager: RuntimeStateManager,
) -> dict[str, UnitRuntimeState]:
	"""Activate and claim the first unit for verification."""
	states = state_manager.initialize(contract)
	states = state_manager.activate(contract, states, contract.units[0].unit_id)
	return state_manager.claim_completion(contract, states, contract.units[0].unit_id)


def test_verification_evidence_is_frozen_and_copies_nested_mappings() -> None:
	expected = {'target.key': 'company_a/job_123'}
	observed = {'job_id': 'job_123'}
	evidence = VerificationEvidence(
		expected=expected,
		observed=observed,
		source=VerificationSource.BROWSER,
		summary='found target application',
	)
	expected['target.key'] = 'changed'
	observed['job_id'] = 'changed'

	assert evidence.expected == {'target.key': 'company_a/job_123'}
	assert evidence.observed == {'job_id': 'job_123'}
	with pytest.raises(TypeError):
		cast(Mapping[str, str], evidence.observed)['job_id'] = 'mutated'  # type: ignore[index]


def test_expected_target_evidence_is_namespaced_and_deterministic() -> None:
	unit = make_unit()

	assert expected_target_evidence(unit) == {
		'target.type': 'job',
		'target.key': 'company_a/job_123',
		'target.attributes.company': 'Company A',
		'target.attributes.job_id': 'job_123',
	}


def test_runtime_state_machine_requires_each_preceding_status() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	manager = RuntimeStateManager()
	states = manager.initialize(contract)

	with pytest.raises(RuntimeStateTransitionError, match='ACTIVE'):
		manager.claim_completion(contract, states, unit.unit_id)

	states = manager.activate(contract, states, unit.unit_id)
	states = manager.claim_completion(contract, states, unit.unit_id)
	assert states[unit.unit_id].status is UnitStatus.COMPLETION_CANDIDATE
	with pytest.raises(RuntimeStateTransitionError, match='COMPLETION_CANDIDATE'):
		manager.claim_completion(contract, states, unit.unit_id)

	states = manager.begin_verification(contract, states, unit.unit_id)
	assert states[unit.unit_id].status is UnitStatus.VERIFYING
	assert states[unit.unit_id].verification_status is VerificationStatus.CHECKING
	with pytest.raises(RuntimeStateTransitionError, match='COMPLETION_CANDIDATE'):
		manager.begin_verification(contract, states, unit.unit_id)


@pytest.mark.parametrize(
	'outcome, expected_status, expected_verification, expected_effect',
	[
		(
			VerificationOutcome.VERIFIED,
			UnitStatus.COMPLETED,
			VerificationStatus.VERIFIED,
			EffectStatus.COMMITTED,
		),
		(
			VerificationOutcome.REJECTED,
			UnitStatus.ACTIVE,
			VerificationStatus.REJECTED,
			EffectStatus.NOT_APPLIED,
		),
		(
			VerificationOutcome.INCONCLUSIVE,
			UnitStatus.UNKNOWN,
			VerificationStatus.INCONCLUSIVE,
			EffectStatus.UNKNOWN,
		),
	],
)
def test_verification_outcomes_drive_the_only_legal_terminal_mapping(
	outcome: VerificationOutcome,
	expected_status: UnitStatus,
	expected_verification: VerificationStatus,
	expected_effect: EffectStatus,
) -> None:
	unit = make_unit()
	contract = make_contract(unit)
	manager = RuntimeStateManager()
	original = candidate_states(contract, manager)
	verifying = manager.begin_verification(contract, original, unit.unit_id)

	updated = manager.apply_verification_result(contract, verifying, unit.unit_id, make_result(unit, outcome))

	assert updated[unit.unit_id].status is expected_status
	assert updated[unit.unit_id].verification_status is expected_verification
	assert updated[unit.unit_id].effect_status is expected_effect
	assert original[unit.unit_id].status is UnitStatus.COMPLETION_CANDIDATE


def test_verified_read_only_unit_uses_not_applicable_effect_status() -> None:
	unit = make_unit(has_side_effect=False)
	contract = make_contract(unit)
	manager = RuntimeStateManager()
	states = manager.initialize(contract)
	assert states[unit.unit_id].effect_status is EffectStatus.NOT_APPLICABLE
	states = manager.activate(contract, states, unit.unit_id)
	states = manager.claim_completion(contract, states, unit.unit_id)
	states = manager.begin_verification(contract, states, unit.unit_id)

	updated = manager.apply_verification_result(
		contract,
		states,
		unit.unit_id,
		make_result(unit, VerificationOutcome.VERIFIED),
	)

	assert updated[unit.unit_id].status is UnitStatus.COMPLETED
	assert updated[unit.unit_id].effect_status is EffectStatus.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_verification_manager_rejects_a_non_observational_verifier_before_transition() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	state_manager = RuntimeStateManager()
	states = candidate_states(contract, state_manager)
	manager: VerificationManager[str] = VerificationManager(state_manager)

	with pytest.raises(VerificationError, match='observational'):
		await manager.verify_candidate(
			contract,
			states,
			unit.unit_id,
			MutatingVerifier(make_result(unit, VerificationOutcome.VERIFIED)),
			'browser snapshot',
		)

	assert states[unit.unit_id].status is UnitStatus.COMPLETION_CANDIDATE


@pytest.mark.asyncio
async def test_verification_manager_applies_a_structured_result() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	state_manager = RuntimeStateManager()
	states = candidate_states(contract, state_manager)
	manager: VerificationManager[str] = VerificationManager(state_manager)

	updated, result = await manager.verify_candidate(
		contract,
		states,
		unit.unit_id,
		StaticVerifier(make_result(unit, VerificationOutcome.VERIFIED)),
		'browser snapshot',
	)

	assert result.outcome is VerificationOutcome.VERIFIED
	assert updated[unit.unit_id].status is UnitStatus.COMPLETED
	assert states[unit.unit_id].status is UnitStatus.COMPLETION_CANDIDATE


@pytest.mark.asyncio
async def test_verified_claim_with_mismatched_observed_target_becomes_inconclusive() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	state_manager = RuntimeStateManager()
	states = candidate_states(contract, state_manager)
	manager: VerificationManager[str] = VerificationManager(state_manager)
	result = VerificationResult(
		outcome=VerificationOutcome.VERIFIED,
		evidence=VerificationEvidence(
			expected=expected_target_evidence(unit),
			observed={'job_id': 'job_456', 'company': 'Company A', 'status': 'submitted'},
			source=VerificationSource.BROWSER,
			summary='a different submitted application was found',
		),
	)

	updated, checked_result = await manager.verify_candidate(
		contract,
		states,
		unit.unit_id,
		StaticVerifier(result),
		'browser snapshot',
	)

	assert checked_result.outcome is VerificationOutcome.INCONCLUSIVE
	assert 'observed target identity' in checked_result.detail
	assert updated[unit.unit_id].status is UnitStatus.UNKNOWN


@pytest.mark.asyncio
async def test_verifier_failure_becomes_inconclusive_unknown_with_evidence() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	state_manager = RuntimeStateManager()
	states = candidate_states(contract, state_manager)
	manager: VerificationManager[str] = VerificationManager(state_manager)

	updated, result = await manager.verify_candidate(
		contract,
		states,
		unit.unit_id,
		FailingVerifier(),
		'browser snapshot',
	)

	assert result.outcome is VerificationOutcome.INCONCLUSIVE
	assert result.evidence.expected == expected_target_evidence(unit)
	assert result.evidence.observed == {}
	assert 'TimeoutError' in result.detail
	assert updated[unit.unit_id].status is UnitStatus.UNKNOWN
	assert updated[unit.unit_id].effect_status is EffectStatus.UNKNOWN


@pytest.mark.asyncio
async def test_verifier_cancellation_propagates() -> None:
	unit = make_unit()
	contract = make_contract(unit)
	state_manager = RuntimeStateManager()
	states = candidate_states(contract, state_manager)
	manager: VerificationManager[str] = VerificationManager(state_manager)

	with pytest.raises(asyncio.CancelledError):
		await manager.verify_candidate(
			contract,
			states,
			unit.unit_id,
			CancelledVerifier(),
			'browser snapshot',
		)
