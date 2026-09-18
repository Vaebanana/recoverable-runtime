"""Restart bootstrap for durable Recoverable Runtime state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType

from pydantic import field_serializer, field_validator

from browser_use.recovery.contracts import (
	EffectStatus,
	FrozenModel,
	SemanticContract,
	UnitRuntimeState,
	UnitStatus,
	VerificationStatus,
)
from browser_use.recovery.persistence.models import EffectRecord, EffectRecordStatus, WorkflowRun
from browser_use.recovery.persistence.storage import RuntimeStorage, StorageNotFoundError
from browser_use.recovery.scheduler import RuntimeScheduler


class RecoveryBootstrapError(RuntimeError):
	"""Raised when durable state cannot reconstruct a Runtime."""


class RecoveredRuntime(FrozenModel):
	"""Validated business state reconstructed for a new process run."""

	workflow_id: str
	run_id: str
	contract: SemanticContract
	states: Mapping[str, UnitRuntimeState]
	active_unit_id: str | None = None
	last_effect_seq: int
	resumed_from_checkpoint_id: str
	requires_reverification: tuple[str, ...] = ()

	@field_validator('states', mode='after')
	@classmethod
	def freeze_states(cls, value: Mapping[str, UnitRuntimeState]) -> Mapping[str, UnitRuntimeState]:
		"""Copy recovered state into immutable storage."""
		return MappingProxyType(dict(value))

	@field_serializer('states')
	def serialize_states(self, value: Mapping[str, UnitRuntimeState]) -> dict[str, UnitRuntimeState]:
		"""Serialize recovered states through a regular mapping."""
		return dict(value)


def normalize_after_restart(
	contract: SemanticContract,
	states: Mapping[str, UnitRuntimeState],
	effects: Sequence[EffectRecord],
) -> dict[str, UnitRuntimeState]:
	"""Normalize interrupted states without retrying or reconciling effects."""
	working_states = dict(states)
	RuntimeScheduler().ready_unit_ids(contract, working_states)
	latest_effects: dict[str, EffectRecord] = {}
	for record in sorted(effects, key=lambda item: item.seq):
		latest_effects[record.unit_id] = record

	normalized: dict[str, UnitRuntimeState] = {}
	for unit in contract.units:
		state = working_states[unit.unit_id]
		if state.status in {
			UnitStatus.PENDING,
			UnitStatus.COMPLETED,
			UnitStatus.SKIPPED,
			UnitStatus.SUPERSEDED,
			UnitStatus.FAILED,
			UnitStatus.UNKNOWN,
		}:
			normalized[unit.unit_id] = state
			continue

		if state.status in {UnitStatus.COMPLETION_CANDIDATE, UnitStatus.VERIFYING}:
			normalized[unit.unit_id] = _replace_state(
				state,
				status=UnitStatus.COMPLETION_CANDIDATE,
				verification_status=VerificationStatus.NOT_CHECKED,
			)
			continue

		if state.status is not UnitStatus.ACTIVE:
			normalized[unit.unit_id] = state
			continue
		if not unit.effect.has_side_effect:
			normalized[unit.unit_id] = state
			continue

		latest = latest_effects.get(unit.unit_id)
		if latest is None:
			normalized[unit.unit_id] = state
		elif latest.status is EffectRecordStatus.NOT_APPLIED:
			normalized[unit.unit_id] = _replace_state(
				state,
				status=UnitStatus.ACTIVE,
				effect_status=EffectStatus.NOT_APPLIED,
			)
		elif latest.status is EffectRecordStatus.COMMITTED:
			normalized[unit.unit_id] = _replace_state(
				state,
				status=UnitStatus.COMPLETION_CANDIDATE,
				verification_status=VerificationStatus.NOT_CHECKED,
				effect_status=EffectStatus.COMMITTED,
			)
		else:
			normalized[unit.unit_id] = _replace_state(
				state,
				status=UnitStatus.UNKNOWN,
				verification_status=VerificationStatus.INCONCLUSIVE,
				effect_status=EffectStatus.UNKNOWN,
			)
	return normalized


class RecoveryBootstrap:
	"""Reconstruct a new run from the latest consistent checkpoint."""

	def __init__(self, storage: RuntimeStorage, scheduler: RuntimeScheduler | None = None) -> None:
		self._storage = storage
		self._scheduler = scheduler or RuntimeScheduler()

	def restore(self, workflow_id: str, run_id: str) -> RecoveredRuntime:
		"""Load durable facts through the checkpoint boundary and start a new run."""
		checkpoint = self._storage.load_latest_checkpoint(workflow_id)
		if checkpoint is None:
			raise RecoveryBootstrapError(f'no checkpoint exists for workflow {workflow_id}')
		try:
			contract = self._storage.load_contract(
				workflow_id,
				checkpoint.contract_id,
				checkpoint.contract_version,
			)
		except StorageNotFoundError as exc:
			raise RecoveryBootstrapError(f'checkpoint {checkpoint.checkpoint_id} references a missing Contract version') from exc

		effects = self._storage.read_effects(workflow_id, through_seq=checkpoint.last_effect_seq)
		normalized = normalize_after_restart(contract, checkpoint.unit_states, effects)
		self._scheduler.ready_unit_ids(contract, normalized)
		in_flight = [
			unit_id
			for unit_id, state in normalized.items()
			if state.status in {UnitStatus.ACTIVE, UnitStatus.COMPLETION_CANDIDATE, UnitStatus.VERIFYING}
		]
		if len(in_flight) > 1:
			raise RecoveryBootstrapError(f'restored state contains multiple in-flight units: {in_flight}')

		self._storage.start_run(
			WorkflowRun(
				workflow_id=workflow_id,
				run_id=run_id,
				resumed_from_checkpoint_id=checkpoint.checkpoint_id,
			)
		)
		return RecoveredRuntime(
			workflow_id=workflow_id,
			run_id=run_id,
			contract=contract,
			states=normalized,
			active_unit_id=in_flight[0] if in_flight else None,
			last_effect_seq=checkpoint.last_effect_seq,
			resumed_from_checkpoint_id=checkpoint.checkpoint_id,
			requires_reverification=tuple(
				unit_id for unit_id, state in normalized.items() if state.status is UnitStatus.COMPLETION_CANDIDATE
			),
		)


def _replace_state(state: UnitRuntimeState, **changes: object) -> UnitRuntimeState:
	"""Copy one immutable state and refresh its transition timestamp."""
	return state.model_copy(update={**changes, 'updated_at': datetime.now(timezone.utc)})
