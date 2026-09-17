"""Minimal dependency scheduler for semantic units."""

from __future__ import annotations

from browser_use.recovery.contracts import SemanticContract, UnitRuntimeState, UnitStatus


class SchedulingError(ValueError):
	"""Raised when a requested runtime transition is not schedulable."""


class RuntimeScheduler:
	"""Derive readiness and validate single-unit activation."""

	def ready_unit_ids(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
	) -> list[str]:
		"""Return pending units whose dependencies are all completed."""
		self._validate_state_coverage(contract, states)
		completed_unit_ids = {unit_id for unit_id, state in states.items() if state.status == UnitStatus.COMPLETED}
		return [
			unit.unit_id
			for unit in contract.units
			if states[unit.unit_id].status == UnitStatus.PENDING
			and all(dependency in completed_unit_ids for dependency in unit.depends_on)
		]

	def validate_activation(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
	) -> None:
		"""Reject unknown, blocked, or concurrently active unit requests."""
		self._validate_state_coverage(contract, states)
		if unit_id not in states:
			raise SchedulingError(f'unknown unit: {unit_id}')
		active_unit_ids = [state.unit_id for state in states.values() if state.status == UnitStatus.ACTIVE]
		if active_unit_ids and unit_id not in active_unit_ids:
			raise SchedulingError(f'another unit is already active: {active_unit_ids[0]}')
		if unit_id not in self.ready_unit_ids(contract, states) and unit_id not in active_unit_ids:
			raise SchedulingError(f'unit is not ready: {unit_id}')

	def _validate_state_coverage(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
	) -> None:
		"""Require an exact, identity-consistent state snapshot for the Contract."""
		contract_unit_ids = {unit.unit_id for unit in contract.units}
		state_unit_ids = set(states)
		missing = contract_unit_ids - state_unit_ids
		if missing:
			raise SchedulingError(f'missing runtime state for units: {sorted(missing)}')
		unexpected = state_unit_ids - contract_unit_ids
		if unexpected:
			raise SchedulingError(f'unexpected runtime state for units: {sorted(unexpected)}')
		for unit_id, state in states.items():
			if state.unit_id != unit_id:
				raise SchedulingError(f'runtime state key does not match state unit_id: {unit_id!r} != {state.unit_id!r}')
