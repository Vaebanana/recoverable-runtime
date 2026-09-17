"""Identity authority and invariant validation for semantic Contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from browser_use.recovery.contracts import (
	AddUnitOp,
	ContractApplyResult,
	ContractDelta,
	EffectStatus,
	ProposedUnit,
	SemanticContract,
	SemanticUnit,
	SupersedeUnitOp,
	UnitProposal,
	UnitReference,
	UnitRuntimeState,
	UnitStatus,
	UpdateConstraintsOp,
)


class ContractError(ValueError):
	"""Base error for rejected Contract operations."""


class IdentityConflictError(ContractError):
	"""Raised when a proposal misidentifies an existing semantic unit."""


class StaleDeltaError(ContractError):
	"""Raised when a Delta does not target the current Contract version."""


class ContractValidationError(ContractError):
	"""Raised when a Contract would violate a structural invariant."""


class ContractManager:
	"""Assign stable identities and create validated Contract snapshots."""

	def __init__(
		self,
		unit_id_factory: Callable[[], str] | None = None,
		contract_id_factory: Callable[[], str] | None = None,
	) -> None:
		self._unit_id_factory = unit_id_factory or (lambda: f'u_{uuid4().hex}')
		self._contract_id_factory = contract_id_factory or (lambda: f'c_{uuid4().hex}')

	def resolve_proposal(self, contract: SemanticContract, proposal: UnitProposal) -> SemanticUnit | None:
		"""Resolve a proposal by explicit ID, then by exact normalized fingerprint."""
		if proposal.candidate_unit_id is not None:
			try:
				existing = contract.get_unit(proposal.candidate_unit_id)
			except KeyError as exc:
				raise IdentityConflictError(f'unknown candidate_unit_id: {proposal.candidate_unit_id}') from exc
			if existing.identity.fingerprint() != proposal.identity.fingerprint():
				raise IdentityConflictError(f'candidate {proposal.candidate_unit_id} identity does not match existing unit')
			return existing

		fingerprint = proposal.identity.fingerprint()
		matches = [unit for unit in contract.units if unit.identity.fingerprint() == fingerprint]
		if len(matches) > 1:
			raise IdentityConflictError(f'ambiguous fingerprint: {fingerprint!r}')
		return matches[0] if matches else None

	def create_initial_contract(
		self,
		task_id: str,
		proposed_units: list[ProposedUnit],
	) -> SemanticContract:
		"""Create version 1, assigning IDs before resolving temporary dependencies."""
		contract_id = self._contract_id_factory()
		units: list[SemanticUnit] = []
		temp_refs: dict[str, str] = {}

		for item in proposed_units:
			if item.temp_ref in temp_refs:
				raise ContractValidationError(f'duplicate temp_ref: {item.temp_ref}')
			working = SemanticContract(
				contract_id=contract_id,
				task_id=task_id,
				version=1,
				units=tuple(units),
			)
			existing = self.resolve_proposal(working, item.proposal)
			if existing is not None:
				temp_refs[item.temp_ref] = existing.unit_id
				continue
			unit_id = self._unit_id_factory()
			units.append(self._build_unit(unit_id, item.proposal))
			temp_refs[item.temp_ref] = unit_id

		by_id = {unit.unit_id: unit for unit in units}
		for item in proposed_units:
			unit_id = temp_refs[item.temp_ref]
			resolved_dependencies = tuple(self._resolve_reference(reference, temp_refs, units) for reference in item.depends_on)
			unit = by_id[unit_id]
			if unit.depends_on and unit.depends_on != resolved_dependencies:
				raise ContractValidationError(f'conflicting dependency declarations for deduplicated unit {unit_id}')
			if resolved_dependencies:
				by_id[unit_id] = unit.model_copy(update={'depends_on': resolved_dependencies})

		contract = SemanticContract(
			contract_id=contract_id,
			task_id=task_id,
			version=1,
			units=tuple(by_id[unit.unit_id] for unit in units),
		)
		self._validate_contract(contract)
		return contract

	def apply_delta(
		self,
		contract: SemanticContract,
		runtime_states: dict[str, UnitRuntimeState],
		delta: ContractDelta,
	) -> ContractApplyResult:
		"""Validate and atomically apply a Delta to an immutable snapshot."""
		if delta.contract_id != contract.contract_id:
			raise ContractValidationError('delta contract_id does not match current contract')
		if delta.base_version != contract.version:
			raise StaleDeltaError(f'delta based on v{delta.base_version}, current contract is v{contract.version}')
		self._validate_runtime_state_coverage(contract, runtime_states)

		units = list(contract.units)
		runtime_superseded_unit_ids = {
			unit_id for unit_id, state in runtime_states.items() if state.status == UnitStatus.SUPERSEDED
		}
		temp_refs: dict[str, str] = {}
		added_unit_ids: list[str] = []
		superseded_units: dict[str, str] = {}

		for operation in delta.operations:
			working_contract = contract.model_copy(update={'units': tuple(units)})

			if isinstance(operation, AddUnitOp):
				if operation.temp_ref in temp_refs:
					raise ContractValidationError(f'duplicate temp_ref: {operation.temp_ref}')
				existing = self.resolve_proposal(working_contract, operation.proposal)
				if existing is not None:
					self._ensure_referenceable_unit(
						existing.unit_id,
						runtime_superseded_unit_ids | set(superseded_units),
					)
					temp_refs[operation.temp_ref] = existing.unit_id
					continue
				dependencies = tuple(
					self._resolve_reference(
						reference,
						temp_refs,
						units,
						runtime_superseded_unit_ids | set(superseded_units),
					)
					for reference in operation.depends_on
				)
				unit_id = self._unit_id_factory()
				units.append(self._build_unit(unit_id, operation.proposal, dependencies))
				temp_refs[operation.temp_ref] = unit_id
				added_unit_ids.append(unit_id)
				continue

			if isinstance(operation, UpdateConstraintsOp):
				if operation.unit_id in superseded_units:
					raise ContractValidationError(
						f'unit {operation.unit_id} definition is frozen because it was already superseded in this delta'
					)
				index = self._find_index(units, operation.unit_id)
				self._ensure_definition_mutable(operation.unit_id, runtime_states)
				unit = units[index]
				update: dict[str, object] = {}
				if operation.depends_on is not None:
					update['depends_on'] = tuple(
						self._resolve_reference(
							reference,
							temp_refs,
							units,
							runtime_superseded_unit_ids | set(superseded_units),
						)
						for reference in operation.depends_on
					)
				if operation.preconditions is not None:
					update['preconditions'] = operation.preconditions
				if operation.postconditions is not None:
					update['postconditions'] = operation.postconditions
				if operation.effect is not None:
					update['effect'] = operation.effect
				if operation.verification is not None:
					update['verification'] = operation.verification
				updated_unit = unit.model_copy(update=update)
				if updated_unit != unit:
					units[index] = updated_unit
				continue

			if isinstance(operation, SupersedeUnitOp):
				if operation.old_unit_id in superseded_units:
					raise ContractValidationError(f'unit {operation.old_unit_id} was already superseded in this delta')
				old_index = self._find_index(units, operation.old_unit_id)
				self._ensure_supersedable(operation.old_unit_id, runtime_states)
				old_unit = units[old_index]
				if old_unit.identity.fingerprint() == operation.replacement.identity.fingerprint():
					raise ContractValidationError('supersede replacement must have a different semantic identity')
				if self.resolve_proposal(working_contract, operation.replacement) is not None:
					raise ContractValidationError('supersede replacement duplicates an existing unit identity')

				replacement_id = self._unit_id_factory()
				if operation.depends_on is None:
					replacement_dependencies = old_unit.depends_on
				else:
					replacement_dependencies = tuple(
						self._resolve_reference(
							reference,
							temp_refs,
							units,
							runtime_superseded_unit_ids | set(superseded_units),
						)
						for reference in operation.depends_on
					)
				units.append(
					self._build_unit(
						replacement_id,
						operation.replacement,
						replacement_dependencies,
					)
				)
				superseded_units[old_unit.unit_id] = replacement_id
				added_unit_ids.append(replacement_id)

				rewired_units: list[SemanticUnit] = []
				for item in units:
					if old_unit.unit_id in item.depends_on:
						dependencies = tuple(
							replacement_id if dependency == old_unit.unit_id else dependency for dependency in item.depends_on
						)
						item = item.model_copy(update={'depends_on': dependencies})
					rewired_units.append(item)
				units = rewired_units
				continue

			raise ContractValidationError(f'unsupported delta operation: {type(operation)!r}')

		final_units = tuple(units)
		if final_units == contract.units:
			return ContractApplyResult(contract=contract)

		new_contract = contract.model_copy(
			update={
				'version': contract.version + 1,
				'previous_version': contract.version,
				'units': final_units,
				'created_at': datetime.now(timezone.utc),
			}
		)
		self._validate_contract(new_contract)
		return ContractApplyResult(
			contract=new_contract,
			added_unit_ids=tuple(added_unit_ids),
			superseded_units=superseded_units,
		)

	def _build_unit(
		self,
		unit_id: str,
		proposal: UnitProposal,
		depends_on: tuple[str, ...] = (),
	) -> SemanticUnit:
		"""Bind a Runtime-owned ID to a validated proposal."""
		return SemanticUnit(
			unit_id=unit_id,
			identity=proposal.identity,
			intent=proposal.intent,
			target=proposal.target,
			depends_on=depends_on,
			preconditions=proposal.preconditions,
			postconditions=proposal.postconditions,
			effect=proposal.effect,
			verification=proposal.verification,
		)

	def _ensure_definition_mutable(
		self,
		unit_id: str,
		runtime_states: dict[str, UnitRuntimeState],
	) -> None:
		"""Freeze completed definitions and every unit whose effect has started."""
		state = runtime_states[unit_id]
		if state.status in {UnitStatus.COMPLETED, UnitStatus.SUPERSEDED}:
			raise ContractValidationError(f'{state.status} unit {unit_id} definition is frozen')
		if state.effect_status not in {EffectStatus.NOT_STARTED, EffectStatus.NOT_APPLICABLE}:
			raise ContractValidationError(f'unit {unit_id} definition is frozen after effect execution starts')

	def _ensure_supersedable(
		self,
		unit_id: str,
		runtime_states: dict[str, UnitRuntimeState],
	) -> None:
		"""Reject replacement when an old unit may already have external effects."""
		state = runtime_states[unit_id]
		if state.effect_status not in {EffectStatus.NOT_STARTED, EffectStatus.NOT_APPLICABLE}:
			raise ContractValidationError(f'unit {unit_id} cannot be superseded after effect execution starts')
		if state.status in {UnitStatus.COMPLETED, UnitStatus.UNKNOWN, UnitStatus.SUPERSEDED}:
			raise ContractValidationError(f'unit {unit_id} cannot be superseded from state {state.status}')

	def _resolve_reference(
		self,
		reference: UnitReference,
		temp_refs: dict[str, str],
		units: list[SemanticUnit],
		unavailable_unit_ids: set[str] | None = None,
	) -> str:
		"""Resolve a stable or temporary reference against the working snapshot."""
		if reference.unit_id is not None:
			self._find_index(units, reference.unit_id)
			resolved_unit_id = reference.unit_id
		else:
			assert reference.temp_ref is not None
			if reference.temp_ref not in temp_refs:
				raise ContractValidationError(f'unknown temp_ref: {reference.temp_ref}')
			resolved_unit_id = temp_refs[reference.temp_ref]
		self._ensure_referenceable_unit(resolved_unit_id, unavailable_unit_ids or set())
		return resolved_unit_id

	def _ensure_referenceable_unit(self, unit_id: str, unavailable_unit_ids: set[str]) -> None:
		"""Reject identity bindings and dependencies to terminal superseded units."""
		if unit_id in unavailable_unit_ids:
			raise ContractValidationError(f'superseded unit cannot be referenced: {unit_id}')

	def _find_index(self, units: list[SemanticUnit], unit_id: str) -> int:
		"""Find a unit in a working snapshot or reject the reference."""
		for index, unit in enumerate(units):
			if unit.unit_id == unit_id:
				return index
		raise ContractValidationError(f'unknown unit_id: {unit_id}')

	def _validate_runtime_state_coverage(
		self,
		contract: SemanticContract,
		runtime_states: dict[str, UnitRuntimeState],
	) -> None:
		"""Require an exact, identity-consistent state snapshot for the Contract."""
		contract_unit_ids = {unit.unit_id for unit in contract.units}
		state_unit_ids = set(runtime_states)
		missing = contract_unit_ids - state_unit_ids
		if missing:
			raise ContractValidationError(f'missing runtime state for units: {sorted(missing)}')
		unexpected = state_unit_ids - contract_unit_ids
		if unexpected:
			raise ContractValidationError(f'unexpected runtime state for units: {sorted(unexpected)}')
		for unit_id, state in runtime_states.items():
			if state.unit_id != unit_id:
				raise ContractValidationError(f'runtime state key does not match state unit_id: {unit_id!r} != {state.unit_id!r}')

	def _validate_contract(self, contract: SemanticContract) -> None:
		"""Validate uniqueness, dependency integrity, and DAG acyclicity."""
		unit_ids = [unit.unit_id for unit in contract.units]
		if len(unit_ids) != len(set(unit_ids)):
			raise ContractValidationError('duplicate unit_id')

		known_unit_ids = set(unit_ids)
		fingerprints: set[tuple[str, str, str]] = set()
		for unit in contract.units:
			fingerprint = unit.identity.fingerprint()
			if fingerprint in fingerprints:
				raise ContractValidationError(f'duplicate semantic fingerprint: {fingerprint!r}')
			fingerprints.add(fingerprint)
			if unit.unit_id in unit.depends_on:
				raise ContractValidationError(f'unit {unit.unit_id} cannot depend on itself')
			missing = set(unit.depends_on) - known_unit_ids
			if missing:
				raise ContractValidationError(f'unit {unit.unit_id} has missing dependencies: {sorted(missing)}')

		self._validate_acyclic(contract)

	def _validate_acyclic(self, contract: SemanticContract) -> None:
		"""Reject dependency cycles using a depth-first traversal."""
		dependencies = {unit.unit_id: set(unit.depends_on) for unit in contract.units}
		visiting: set[str] = set()
		visited: set[str] = set()

		def visit(unit_id: str) -> None:
			if unit_id in visited:
				return
			if unit_id in visiting:
				raise ContractValidationError('dependency cycle detected')
			visiting.add(unit_id)
			for dependency in dependencies[unit_id]:
				visit(dependency)
			visiting.remove(unit_id)
			visited.add(unit_id)

		for unit_id in dependencies:
			visit(unit_id)
