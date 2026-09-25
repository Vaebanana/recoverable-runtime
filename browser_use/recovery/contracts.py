"""Pydantic schemas for versioned semantic task contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


def _empty_string_mapping() -> Mapping[str, str]:
	"""Create a new immutable empty mapping for model defaults."""
	return MappingProxyType({})


def _normalize_identity_key(value: str) -> str:
	"""Normalize a canonical identity component for exact comparison."""
	return value.strip().lower()


class FrozenModel(BaseModel):
	"""Base model for immutable, strictly validated Contract data."""

	model_config = ConfigDict(frozen=True, extra='forbid')


class Idempotency(StrEnum):
	"""Whether repeating a unit's external effect is safe."""

	NOT_APPLICABLE = 'not_applicable'
	IDEMPOTENT = 'idempotent'
	NON_IDEMPOTENT = 'non_idempotent'
	UNKNOWN = 'unknown'


class Reversibility(StrEnum):
	"""Whether a unit's external effect can be undone."""

	NOT_APPLICABLE = 'not_applicable'
	REVERSIBLE = 'reversible'
	IRREVERSIBLE = 'irreversible'
	UNKNOWN = 'unknown'


class VerificationSource(StrEnum):
	"""Evidence source used to verify a unit's postconditions."""

	BROWSER = 'browser'
	EXTERNAL_TOOL = 'external_tool'
	MIXED = 'mixed'


class UnitStatus(StrEnum):
	"""Lifecycle state of one semantic unit."""

	PENDING = 'pending'
	ACTIVE = 'active'
	COMPLETION_CANDIDATE = 'completion_candidate'
	VERIFYING = 'verifying'
	COMPLETED = 'completed'
	SKIPPED = 'skipped'
	SUPERSEDED = 'superseded'
	FAILED = 'failed'
	UNKNOWN = 'unknown'


class VerificationStatus(StrEnum):
	"""Current result of postcondition verification."""

	NOT_CHECKED = 'not_checked'
	CHECKING = 'checking'
	VERIFIED = 'verified'
	REJECTED = 'rejected'
	INCONCLUSIVE = 'inconclusive'


class EffectStatus(StrEnum):
	"""Observed execution state of a unit's external effect."""

	NOT_APPLICABLE = 'not_applicable'
	NOT_STARTED = 'not_started'
	ATTEMPTED = 'attempted'
	COMMITTED = 'committed'
	NOT_APPLIED = 'not_applied'
	UNKNOWN = 'unknown'


class TargetSpec(FrozenModel):
	"""Stable business target addressed by a semantic unit."""

	type: str
	key: str
	attributes: Mapping[str, str] = Field(default_factory=_empty_string_mapping)

	@field_validator('attributes', mode='after')
	@classmethod
	def freeze_attributes(cls, value: Mapping[str, str]) -> Mapping[str, str]:
		"""Copy target attributes into an immutable mapping."""
		return MappingProxyType(dict(value))

	@field_serializer('attributes')
	def serialize_attributes(self, value: Mapping[str, str]) -> dict[str, str]:
		"""Serialize immutable attributes through the public dict schema."""
		return dict(value)


class ConditionSpec(FrozenModel):
	"""Human-readable precondition or postcondition."""

	description: str
	expected_observation: str | None = None
	negative_observation: str | None = None


class EffectSpec(FrozenModel):
	"""External side-effect characteristics used by recovery policy."""

	has_side_effect: bool
	idempotency: Idempotency
	reversibility: Reversibility
	description: str | None = None


class VerificationSpec(FrozenModel):
	"""Declarative procedure for proving a unit's postconditions."""

	required: bool = True
	source: VerificationSource
	procedure: str
	observation_url: str | None = None
	observation_is_read_only: bool = False


class UnitIdentity(FrozenModel):
	"""Canonical semantic keys used only for exact candidate matching."""

	intent_key: str
	target_key: str
	outcome_key: str

	def fingerprint(self) -> tuple[str, str, str]:
		"""Return the deterministic exact-match fingerprint."""
		return (
			_normalize_identity_key(self.intent_key),
			_normalize_identity_key(self.target_key),
			_normalize_identity_key(self.outcome_key),
		)


class UnitProposal(FrozenModel):
	"""LLM-proposed semantics before the Runtime resolves identity."""

	candidate_unit_id: str | None = None
	identity: UnitIdentity
	intent: str
	target: TargetSpec
	preconditions: tuple[ConditionSpec, ...] = ()
	postconditions: tuple[ConditionSpec, ...] = ()
	effect: EffectSpec
	verification: VerificationSpec

	@model_validator(mode='after')
	def target_identity_matches_target(self) -> UnitProposal:
		"""Reject proposals whose canonical target contradicts their target data."""
		if _normalize_identity_key(self.identity.target_key) != _normalize_identity_key(self.target.key):
			raise ValueError('identity target_key must match target key')
		return self


class UnitReference(FrozenModel):
	"""Reference either a stable Runtime ID or a Delta-local temporary ID."""

	unit_id: str | None = None
	temp_ref: str | None = None

	@model_validator(mode='after')
	def exactly_one_reference(self) -> UnitReference:
		"""Reject missing or ambiguous unit references."""
		if (self.unit_id is None) == (self.temp_ref is None):
			raise ValueError('exactly one of unit_id or temp_ref is required')
		return self


class ProposedUnit(FrozenModel):
	"""Initial unit proposal with temporary dependency references."""

	temp_ref: str
	proposal: UnitProposal
	depends_on: tuple[UnitReference, ...] = ()


class SemanticUnit(FrozenModel):
	"""Stable semantic definition stored in a Contract snapshot."""

	unit_id: str
	identity: UnitIdentity
	intent: str
	target: TargetSpec
	depends_on: tuple[str, ...] = ()
	preconditions: tuple[ConditionSpec, ...] = ()
	postconditions: tuple[ConditionSpec, ...] = ()
	effect: EffectSpec
	verification: VerificationSpec

	@model_validator(mode='after')
	def target_identity_matches_target(self) -> SemanticUnit:
		"""Reject persisted units whose canonical target contradicts their target data."""
		if _normalize_identity_key(self.identity.target_key) != _normalize_identity_key(self.target.key):
			raise ValueError('identity target_key must match target key')
		return self


class SemanticContract(FrozenModel):
	"""Immutable versioned snapshot of a task's semantic definition."""

	schema_version: str = '0.1'
	contract_id: str
	task_id: str
	version: int
	previous_version: int | None = None
	units: tuple[SemanticUnit, ...] = ()
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

	def get_unit(self, unit_id: str) -> SemanticUnit:
		"""Return one stable unit or raise ``KeyError`` when it is absent."""
		for unit in self.units:
			if unit.unit_id == unit_id:
				return unit
		raise KeyError(unit_id)


class UnitRuntimeState(FrozenModel):
	"""Execution state stored separately from semantic definitions."""

	unit_id: str
	status: UnitStatus = UnitStatus.PENDING
	verification_status: VerificationStatus = VerificationStatus.NOT_CHECKED
	effect_status: EffectStatus = EffectStatus.NOT_STARTED
	superseded_by: str | None = None
	updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AddUnitOp(FrozenModel):
	"""Add a new proposal, or bind its temporary reference to an exact match."""

	op: Literal['add_unit'] = 'add_unit'
	temp_ref: str
	proposal: UnitProposal
	depends_on: tuple[UnitReference, ...] = ()


class UpdateConstraintsOp(FrozenModel):
	"""Update versioned constraints without exposing identity fields."""

	op: Literal['update_constraints'] = 'update_constraints'
	unit_id: str
	depends_on: tuple[UnitReference, ...] | None = None
	preconditions: tuple[ConditionSpec, ...] | None = None
	postconditions: tuple[ConditionSpec, ...] | None = None
	effect: EffectSpec | None = None
	verification: VerificationSpec | None = None


class SupersedeUnitOp(FrozenModel):
	"""Replace one stable definition with a newly identified unit."""

	op: Literal['supersede_unit'] = 'supersede_unit'
	old_unit_id: str
	replacement: UnitProposal
	depends_on: tuple[UnitReference, ...] | None = None


DeltaOperation = Annotated[
	AddUnitOp | UpdateConstraintsOp | SupersedeUnitOp,
	Field(discriminator='op'),
]


class ContractDelta(FrozenModel):
	"""Optimistically versioned set of atomic semantic changes."""

	delta_id: str | None = None
	contract_id: str
	base_version: int
	operations: tuple[DeltaOperation, ...] = ()
	reason: str
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContractApplyResult(FrozenModel):
	"""New Contract snapshot plus its runtime-state handoff metadata."""

	contract: SemanticContract
	added_unit_ids: tuple[str, ...] = ()
	superseded_units: Mapping[str, str] = Field(default_factory=_empty_string_mapping)

	@field_validator('superseded_units', mode='after')
	@classmethod
	def freeze_superseded_units(cls, value: Mapping[str, str]) -> Mapping[str, str]:
		"""Copy supersede handoff metadata into an immutable mapping."""
		return MappingProxyType(dict(value))

	@field_serializer('superseded_units')
	def serialize_superseded_units(self, value: Mapping[str, str]) -> dict[str, str]:
		"""Serialize immutable handoff metadata through the public dict schema."""
		return dict(value)
