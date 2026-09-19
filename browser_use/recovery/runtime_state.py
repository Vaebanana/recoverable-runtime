"""Copy-on-write helpers for runtime state outside Contract snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from browser_use.recovery.contracts import (
	ContractApplyResult,
	EffectStatus,
	SemanticContract,
	SemanticUnit,
	UnitRuntimeState,
	UnitStatus,
	VerificationStatus,
)
from browser_use.recovery.scheduler import RuntimeScheduler
from browser_use.recovery.verification import VerificationOutcome, VerificationResult


class RuntimeStateTransitionError(ValueError):
	"""Raised when a runtime state transition violates the lifecycle."""


class RuntimeStateManager:
	"""Initialize and transition per-unit runtime state without versioning Contract."""

	def __init__(self, scheduler: RuntimeScheduler | None = None) -> None:
		self._scheduler = scheduler or RuntimeScheduler()

	def initialize(self, contract: SemanticContract) -> dict[str, UnitRuntimeState]:
		"""Create pending state for each unit in a Contract snapshot."""
		return {unit.unit_id: self._initial_state(unit) for unit in contract.units}

	def apply_contract_result(
		self,
		states: dict[str, UnitRuntimeState],
		result: ContractApplyResult,
	) -> dict[str, UnitRuntimeState]:
		"""Initialize added units and mark superseded units on a copied state map."""
		updated = dict(states)
		now = datetime.now(timezone.utc)

		for unit_id in result.added_unit_ids:
			updated.setdefault(unit_id, self._initial_state(result.contract.get_unit(unit_id)))

		for old_unit_id, replacement_id in result.superseded_units.items():
			old_state = updated[old_unit_id]
			updated[old_unit_id] = old_state.model_copy(
				update={
					'status': UnitStatus.SUPERSEDED,
					'superseded_by': replacement_id,
					'updated_at': now,
				}
			)

		return updated

	def claim_completion(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
	) -> dict[str, UnitRuntimeState]:
		"""Record a model completion claim without granting completed status."""
		state = self._state_for_transition(contract, states, unit_id)
		if state.status is not UnitStatus.ACTIVE:
			raise RuntimeStateTransitionError(f'completion claim requires ACTIVE status, got {state.status.name} for {unit_id}')
		return self._replace_state(states, state, status=UnitStatus.COMPLETION_CANDIDATE)

	def begin_verification(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
	) -> dict[str, UnitRuntimeState]:
		"""Move one completion candidate into active verification."""
		state = self._state_for_transition(contract, states, unit_id)
		if state.status is not UnitStatus.COMPLETION_CANDIDATE:
			raise RuntimeStateTransitionError(
				f'verification requires COMPLETION_CANDIDATE status, got {state.status.name} for {unit_id}'
			)
		return self._replace_state(
			states,
			state,
			status=UnitStatus.VERIFYING,
			verification_status=VerificationStatus.CHECKING,
		)

	def apply_verification_result(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
		result: VerificationResult,
	) -> dict[str, UnitRuntimeState]:
		"""Apply the only legal status/effect mapping for a verification outcome."""
		state = self._state_for_transition(contract, states, unit_id)
		if state.status is not UnitStatus.VERIFYING or state.verification_status is not VerificationStatus.CHECKING:
			raise RuntimeStateTransitionError(f'verification result requires VERIFYING/CHECKING status for {unit_id}')

		unit = contract.get_unit(unit_id)
		if result.outcome is VerificationOutcome.VERIFIED:
			return self._replace_state(
				states,
				state,
				status=UnitStatus.COMPLETED,
				verification_status=VerificationStatus.VERIFIED,
				effect_status=(EffectStatus.COMMITTED if unit.effect.has_side_effect else EffectStatus.NOT_APPLICABLE),
			)
		if result.outcome is VerificationOutcome.REJECTED:
			return self._replace_state(
				states,
				state,
				status=UnitStatus.ACTIVE,
				verification_status=VerificationStatus.REJECTED,
				effect_status=EffectStatus.NOT_APPLIED,
			)
		return self._replace_state(
			states,
			state,
			status=UnitStatus.UNKNOWN,
			verification_status=VerificationStatus.INCONCLUSIVE,
			effect_status=EffectStatus.UNKNOWN,
		)

	def apply_reconciliation_result(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
		result: VerificationResult,
	) -> dict[str, UnitRuntimeState]:
		"""Resolve only a genuinely inconclusive UNKNOWN side effect."""
		state = self._state_for_transition(contract, states, unit_id)
		if (
			state.status is not UnitStatus.UNKNOWN
			or state.effect_status is not EffectStatus.UNKNOWN
			or state.verification_status is not VerificationStatus.INCONCLUSIVE
		):
			raise RuntimeStateTransitionError(f'reconciliation requires UNKNOWN/UNKNOWN/INCONCLUSIVE status for {unit_id}')

		if result.outcome is VerificationOutcome.VERIFIED:
			return self._replace_state(
				states,
				state,
				status=UnitStatus.COMPLETED,
				verification_status=VerificationStatus.VERIFIED,
				effect_status=EffectStatus.COMMITTED,
			)
		if result.outcome is VerificationOutcome.REJECTED:
			return self._replace_state(
				states,
				state,
				status=UnitStatus.ACTIVE,
				verification_status=VerificationStatus.REJECTED,
				effect_status=EffectStatus.NOT_APPLIED,
			)
		return self._replace_state(
			states,
			state,
			status=UnitStatus.UNKNOWN,
			verification_status=VerificationStatus.INCONCLUSIVE,
			effect_status=EffectStatus.UNKNOWN,
		)

	def activate(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
	) -> dict[str, UnitRuntimeState]:
		"""Activate one eligible unit and return a copied state map."""
		self._scheduler.validate_activation(contract, states, unit_id)
		updated = dict(states)
		updated[unit_id] = states[unit_id].model_copy(
			update={
				'status': UnitStatus.ACTIVE,
				'updated_at': datetime.now(timezone.utc),
			}
		)
		return updated

	def _state_for_transition(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
	) -> UnitRuntimeState:
		"""Validate a complete snapshot and return the addressed state."""
		self._scheduler.ready_unit_ids(contract, states)
		try:
			contract.get_unit(unit_id)
			return states[unit_id]
		except KeyError as exc:
			raise RuntimeStateTransitionError(f'unknown unit: {unit_id}') from exc

	@staticmethod
	def _initial_state(unit: SemanticUnit) -> UnitRuntimeState:
		"""Create a unit state with an effect status matching its semantics."""
		return UnitRuntimeState(
			unit_id=unit.unit_id,
			effect_status=(EffectStatus.NOT_STARTED if unit.effect.has_side_effect else EffectStatus.NOT_APPLICABLE),
		)

	@staticmethod
	def _replace_state(
		states: dict[str, UnitRuntimeState],
		state: UnitRuntimeState,
		**changes: object,
	) -> dict[str, UnitRuntimeState]:
		"""Replace one immutable state in a copied snapshot."""
		updated = dict(states)
		updated[state.unit_id] = state.model_copy(update={**changes, 'updated_at': datetime.now(timezone.utc)})
		return updated
