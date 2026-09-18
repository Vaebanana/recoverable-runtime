"""Validated durable models for recoverable Runtime persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType

from pydantic import Field, field_serializer, field_validator, model_validator

from browser_use.recovery.contracts import FrozenModel, UnitRuntimeState, UnitStatus


def _empty_mapping() -> Mapping[str, object]:
	"""Return a fresh immutable mapping for model defaults."""
	return MappingProxyType({})


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
	"""Copy a mapping so nested persistence metadata cannot be replaced."""
	return MappingProxyType(dict(value))


class EffectRecordStatus(StrEnum):
	"""One immutable fact in a side-effect attempt history."""

	PREPARED = 'prepared'
	ATTEMPTED = 'attempted'
	COMMITTED = 'committed'
	NOT_APPLIED = 'not_applied'
	UNKNOWN = 'unknown'


class WorkflowRun(FrozenModel):
	"""One process lifetime for a stable workflow identity."""

	workflow_id: str
	run_id: str
	started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
	resumed_from_checkpoint_id: str | None = None


class EffectRecordDraft(FrozenModel):
	"""Effect fact before SQLite assigns its durable sequence."""

	effect_id: str
	workflow_id: str
	run_id: str
	unit_id: str
	effect_key: str
	attempt_id: str
	status: EffectRecordStatus
	idempotency_key: str | None = None
	evidence: Mapping[str, object] | None = None
	metadata: Mapping[str, object] = Field(default_factory=_empty_mapping)
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

	@field_validator('evidence', mode='after')
	@classmethod
	def freeze_evidence(cls, value: Mapping[str, object] | None) -> Mapping[str, object] | None:
		"""Copy optional evidence into immutable storage."""
		return None if value is None else _freeze_mapping(value)

	@field_validator('metadata', mode='after')
	@classmethod
	def freeze_metadata(cls, value: Mapping[str, object]) -> Mapping[str, object]:
		"""Copy effect metadata into immutable storage."""
		return _freeze_mapping(value)

	@field_serializer('evidence')
	def serialize_evidence(self, value: Mapping[str, object] | None) -> dict[str, object] | None:
		"""Serialize evidence through a regular JSON-compatible mapping."""
		return None if value is None else dict(value)

	@field_serializer('metadata')
	def serialize_metadata(self, value: Mapping[str, object]) -> dict[str, object]:
		"""Serialize metadata through a regular JSON-compatible mapping."""
		return dict(value)


class EffectRecord(EffectRecordDraft):
	"""Persisted append-only effect fact with a monotonic sequence."""

	seq: int = Field(ge=1)


class RuntimeCheckpoint(FrozenModel):
	"""Minimal durable business state used to reconstruct a Runtime."""

	checkpoint_id: str
	workflow_id: str
	run_id: str
	contract_id: str
	contract_version: int = Field(ge=1)
	unit_states: Mapping[str, UnitRuntimeState]
	active_unit_id: str | None = None
	last_effect_seq: int = Field(ge=0)
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
	metadata: Mapping[str, object] = Field(default_factory=_empty_mapping)

	@field_validator('unit_states', mode='after')
	@classmethod
	def freeze_unit_states(cls, value: Mapping[str, UnitRuntimeState]) -> Mapping[str, UnitRuntimeState]:
		"""Copy the state snapshot into immutable storage."""
		return MappingProxyType(dict(value))

	@field_validator('metadata', mode='after')
	@classmethod
	def freeze_metadata(cls, value: Mapping[str, object]) -> Mapping[str, object]:
		"""Copy checkpoint metadata into immutable storage."""
		return _freeze_mapping(value)

	@model_validator(mode='after')
	def validate_state_identity_and_active_unit(self) -> RuntimeCheckpoint:
		"""Reject corrupt state keys and invalid in-flight pointers."""
		for unit_id, state in self.unit_states.items():
			if state.unit_id != unit_id:
				raise ValueError(f'state key {unit_id!r} does not match state unit_id {state.unit_id!r}')
		if self.active_unit_id is not None:
			active_state = self.unit_states.get(self.active_unit_id)
			if active_state is None:
				raise ValueError('active_unit_id must reference a persisted unit state')
			if active_state.status not in {
				UnitStatus.ACTIVE,
				UnitStatus.COMPLETION_CANDIDATE,
				UnitStatus.VERIFYING,
			}:
				raise ValueError('active_unit_id must reference an in-flight unit state')
		return self

	@field_serializer('unit_states')
	def serialize_unit_states(self, value: Mapping[str, UnitRuntimeState]) -> dict[str, UnitRuntimeState]:
		"""Serialize immutable state storage through a regular mapping."""
		return dict(value)

	@field_serializer('metadata')
	def serialize_metadata(self, value: Mapping[str, object]) -> dict[str, object]:
		"""Serialize checkpoint metadata through a regular mapping."""
		return dict(value)
