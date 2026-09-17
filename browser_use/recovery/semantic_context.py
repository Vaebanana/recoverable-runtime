"""Typed semantic context supplied to Browser Use at step boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import Field, field_serializer, field_validator

from browser_use.recovery.contracts import (
	FrozenModel,
	Idempotency,
	SemanticContract,
	SemanticUnit,
	UnitRuntimeState,
	UnitStatus,
	VerificationSource,
)
from browser_use.recovery.scheduler import RuntimeScheduler


def _empty_string_mapping() -> Mapping[str, str]:
	"""Create an immutable empty mapping for semantic context."""
	return MappingProxyType({})


class SemanticContextError(ValueError):
	"""Raised when runtime state cannot produce an unambiguous context."""


class SemanticUnitContext(FrozenModel):
	"""Prompt-safe projection of the currently active semantic unit."""

	unit_id: str
	intent: str
	target_type: str
	target_key: str
	target_attributes: Mapping[str, str] = Field(default_factory=_empty_string_mapping)
	postconditions: tuple[str, ...] = ()
	verification_required: bool
	verification_source: VerificationSource
	verification_procedure: str
	has_side_effect: bool
	idempotency: str
	reversibility: str
	effect_warning: str | None = None

	@field_validator('target_attributes', mode='after')
	@classmethod
	def freeze_target_attributes(cls, value: Mapping[str, str]) -> Mapping[str, str]:
		"""Copy target attributes into an immutable prompt projection."""
		return MappingProxyType(dict(value))

	@field_serializer('target_attributes')
	def serialize_target_attributes(self, value: Mapping[str, str]) -> dict[str, str]:
		"""Serialize immutable attributes through a plain mapping."""
		return dict(value)


class SemanticRuntimeContext(FrozenModel):
	"""Complete semantic context for one Browser Use step."""

	contract_id: str
	contract_version: int
	active_unit: SemanticUnitContext | None = None
	ready_unit_ids: tuple[str, ...] = ()
	progress_blocked: bool = False
	blocked_unit_ids: tuple[str, ...] = ()
	block_reason: str | None = None

	def render(self) -> str:
		"""Render a deterministic, concise context message for the model."""
		lines = [
			'Semantic runtime context',
			f'Contract: {self.contract_id} v{self.contract_version}',
		]
		if self.active_unit is None:
			lines.append('Current unit: none')
		else:
			unit = self.active_unit
			lines.append(f'Current unit: {unit.unit_id} — {unit.intent}')
			attributes = ', '.join(f'{key}={value}' for key, value in sorted(unit.target_attributes.items()))
			target = f'Target: {unit.target_type} {unit.target_key}'
			if attributes:
				target += f' ({attributes})'
			lines.extend([target, 'Expected postconditions:'])
			lines.extend(f'- {condition}' for condition in unit.postconditions)
			verification_requirement = 'required' if unit.verification_required else 'optional'
			lines.append(
				f'Verification: {verification_requirement}; source={unit.verification_source.value}; '
				f'procedure={unit.verification_procedure}'
			)
			lines.append(
				f'Effect: side_effect={str(unit.has_side_effect).lower()}; idempotency={unit.idempotency}; '
				f'reversibility={unit.reversibility}'
			)
			if unit.effect_warning is not None:
				lines.append(f'Warning: {unit.effect_warning}')
		lines.append(f'Ready units: {", ".join(self.ready_unit_ids) if self.ready_unit_ids else "none"}')
		lines.append(f'Progress blocked: {"yes" if self.progress_blocked else "no"}')
		if self.progress_blocked:
			lines.append(f'Blocked units: {", ".join(self.blocked_unit_ids)}')
			lines.append(f'Block reason: {self.block_reason}')
		return '\n'.join(lines)


class SemanticContextBuilder:
	"""Derive model context from immutable Contract and runtime snapshots."""

	def __init__(self, scheduler: RuntimeScheduler | None = None) -> None:
		self._scheduler = scheduler or RuntimeScheduler()

	def build(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
	) -> SemanticRuntimeContext:
		"""Build one validated semantic context without mutating Runtime state."""
		ready_unit_ids = tuple(self._scheduler.ready_unit_ids(contract, states))
		active_unit_ids = tuple(unit_id for unit_id, state in states.items() if state.status is UnitStatus.ACTIVE)
		if len(active_unit_ids) > 1:
			raise SemanticContextError(f'multiple ACTIVE units are not allowed: {active_unit_ids}')

		blocked_unit_ids = tuple(unit_id for unit_id, state in states.items() if state.status is UnitStatus.UNKNOWN)
		active_context = self._unit_context(contract.get_unit(active_unit_ids[0])) if active_unit_ids else None
		return SemanticRuntimeContext(
			contract_id=contract.contract_id,
			contract_version=contract.version,
			active_unit=active_context,
			ready_unit_ids=ready_unit_ids,
			progress_blocked=bool(blocked_unit_ids),
			blocked_unit_ids=blocked_unit_ids,
			block_reason=('UNKNOWN unit state requires reconciliation before normal progress' if blocked_unit_ids else None),
		)

	@staticmethod
	def _unit_context(unit: SemanticUnit) -> SemanticUnitContext:
		"""Project a semantic unit into model-facing fields."""
		warning: str | None = None
		if unit.effect.has_side_effect:
			if unit.effect.idempotency is Idempotency.NON_IDEMPOTENT:
				warning = 'non-idempotent side effect'
			elif unit.effect.idempotency is Idempotency.UNKNOWN:
				warning = 'side effect with unknown idempotency'
		return SemanticUnitContext(
			unit_id=unit.unit_id,
			intent=unit.intent,
			target_type=unit.target.type,
			target_key=unit.target.key,
			target_attributes=unit.target.attributes,
			postconditions=tuple(condition.description for condition in unit.postconditions),
			verification_required=unit.verification.required,
			verification_source=unit.verification.source,
			verification_procedure=unit.verification.procedure,
			has_side_effect=unit.effect.has_side_effect,
			idempotency=unit.effect.idempotency.value,
			reversibility=unit.effect.reversibility.value,
			effect_warning=warning,
		)
