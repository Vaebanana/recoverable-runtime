"""Runtime checkpoint construction and persistence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from browser_use.recovery.contracts import SemanticContract, UnitRuntimeState, UnitStatus
from browser_use.recovery.persistence.models import RuntimeCheckpoint
from browser_use.recovery.persistence.storage import RuntimeStorage
from browser_use.recovery.scheduler import RuntimeScheduler


class CheckpointError(ValueError):
	"""Raised when a Runtime snapshot cannot form a valid checkpoint."""


class CheckpointManager:
	"""Build and persist minimal durable Runtime snapshots."""

	def __init__(
		self,
		storage: RuntimeStorage,
		checkpoint_id_factory: Callable[[], str] | None = None,
		scheduler: RuntimeScheduler | None = None,
	) -> None:
		self._storage = storage
		self._checkpoint_id_factory = checkpoint_id_factory or (lambda: f'cp_{uuid4().hex}')
		self._scheduler = scheduler or RuntimeScheduler()

	def build(
		self,
		*,
		workflow_id: str,
		run_id: str,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		last_effect_seq: int,
		metadata: Mapping[str, object] | None = None,
	) -> RuntimeCheckpoint:
		"""Validate and project current Runtime state into a checkpoint."""
		self._scheduler.ready_unit_ids(contract, states)
		in_flight_unit_ids = [
			unit_id
			for unit_id, state in states.items()
			if state.status in {UnitStatus.ACTIVE, UnitStatus.COMPLETION_CANDIDATE, UnitStatus.VERIFYING}
		]
		if len(in_flight_unit_ids) > 1:
			raise CheckpointError(f'multiple in-flight units cannot be checkpointed: {in_flight_unit_ids}')
		return RuntimeCheckpoint(
			checkpoint_id=self._checkpoint_id_factory(),
			workflow_id=workflow_id,
			run_id=run_id,
			contract_id=contract.contract_id,
			contract_version=contract.version,
			unit_states=states,
			active_unit_id=in_flight_unit_ids[0] if in_flight_unit_ids else None,
			last_effect_seq=last_effect_seq,
			metadata=metadata or {},
		)

	def save(
		self,
		*,
		workflow_id: str,
		run_id: str,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		last_effect_seq: int,
		metadata: Mapping[str, object] | None = None,
	) -> RuntimeCheckpoint:
		"""Build and append one durable Runtime checkpoint."""
		checkpoint = self.build(
			workflow_id=workflow_id,
			run_id=run_id,
			contract=contract,
			states=states,
			last_effect_seq=last_effect_seq,
			metadata=metadata,
		)
		self._storage.save_checkpoint(checkpoint)
		return checkpoint

	def load_latest(self, workflow_id: str) -> RuntimeCheckpoint | None:
		"""Load the latest durable checkpoint for a workflow."""
		return self._storage.load_latest_checkpoint(workflow_id)
