"""Reconcile uncertain external side effects without executing them again."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Generic, TypeVar

from pydantic import field_serializer, field_validator

from browser_use.recovery.contracts import (
	EffectStatus,
	FrozenModel,
	SemanticContract,
	UnitRuntimeState,
	UnitStatus,
	VerificationStatus,
)
from browser_use.recovery.persistence.checkpoint import CheckpointManager
from browser_use.recovery.persistence.models import EffectRecord, EffectRecordDraft, EffectRecordStatus
from browser_use.recovery.persistence.storage import RuntimeStorage
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.verification import VerificationManager, VerificationOutcome, VerificationResult, Verifier


class ReconciliationError(ValueError):
	"""Raised when an uncertain effect cannot be reconciled safely."""


class ReconciliationResult(FrozenModel):
	"""Durable result of observing and resolving one uncertain attempt."""

	states: Mapping[str, UnitRuntimeState]
	record: EffectRecord
	verification_result: VerificationResult

	@field_validator('states', mode='after')
	@classmethod
	def freeze_states(cls, value: Mapping[str, UnitRuntimeState]) -> Mapping[str, UnitRuntimeState]:
		"""Copy the returned Runtime snapshot into immutable storage."""
		return MappingProxyType(dict(value))

	@field_serializer('states')
	def serialize_states(self, value: Mapping[str, UnitRuntimeState]) -> dict[str, UnitRuntimeState]:
		"""Serialize immutable states through a regular mapping."""
		return dict(value)


ReconciliationContextT = TypeVar('ReconciliationContextT')


class ReconciliationCoordinator(Generic[ReconciliationContextT]):
	"""Observe and durably resolve one uncertain side-effect attempt."""

	def __init__(
		self,
		*,
		storage: RuntimeStorage,
		checkpoint_manager: CheckpointManager,
		workflow_id: str,
		run_id: str,
		contract: SemanticContract,
		verifier: Verifier[ReconciliationContextT],
		state_manager: RuntimeStateManager | None = None,
	) -> None:
		self._storage = storage
		self._checkpoint_manager = checkpoint_manager
		self._workflow_id = workflow_id
		self._run_id = run_id
		self._contract = contract
		self._verifier = verifier
		self._state_manager = state_manager or RuntimeStateManager()
		self._verification_manager: VerificationManager[ReconciliationContextT] = VerificationManager(self._state_manager)

	async def reconcile(
		self,
		*,
		unit_id: str,
		states: dict[str, UnitRuntimeState],
		last_effect_seq: int,
		context: ReconciliationContextT,
	) -> ReconciliationResult:
		"""Observe one unresolved attempt and atomically persist its resolution."""
		unit = self._contract.get_unit(unit_id)
		self._validate_reconciliation(unit_id, states)
		attempt = self._find_unresolved_attempt(self._storage.read_effects(self._workflow_id), unit_id)
		if attempt is None:
			raise ReconciliationError(f'no unresolved effect attempt exists for unit {unit_id}')

		verification_result = await self._verification_manager.observe(unit, self._verifier, context)
		updated_states = self._state_manager.apply_reconciliation_result(
			self._contract,
			states,
			unit_id,
			verification_result,
		)
		final_status = _effect_record_status(verification_result.outcome)
		checkpoint = self._checkpoint_manager.build(
			workflow_id=self._workflow_id,
			run_id=self._run_id,
			contract=self._contract,
			states=updated_states,
			last_effect_seq=last_effect_seq,
			metadata={
				'effect_phase': final_status.value,
				'attempt_id': attempt.attempt_id,
				'reconciled_effect_seq': attempt.seq,
			},
		)
		record, _ = self._storage.commit_effect_and_checkpoint(
			EffectRecordDraft(
				effect_id=attempt.effect_id,
				workflow_id=self._workflow_id,
				run_id=self._run_id,
				unit_id=unit_id,
				effect_key=attempt.effect_key,
				attempt_id=attempt.attempt_id,
				status=final_status,
				idempotency_key=attempt.idempotency_key,
				evidence=verification_result.model_dump(mode='json'),
				metadata={'reconciled_from_seq': attempt.seq},
			),
			checkpoint,
		)
		return ReconciliationResult(
			states=updated_states,
			record=record,
			verification_result=verification_result,
		)

	@staticmethod
	def _validate_reconciliation(unit_id: str, states: dict[str, UnitRuntimeState]) -> None:
		"""Require the complete state triple that represents uncertainty."""
		try:
			state = states[unit_id]
		except KeyError as exc:
			raise ReconciliationError(f'missing Runtime state for unit {unit_id}') from exc
		if (
			state.status is not UnitStatus.UNKNOWN
			or state.effect_status is not EffectStatus.UNKNOWN
			or state.verification_status is not VerificationStatus.INCONCLUSIVE
		):
			raise ReconciliationError(f'reconciliation requires UNKNOWN/UNKNOWN/INCONCLUSIVE status for {unit_id}')

	@staticmethod
	def _find_unresolved_attempt(records: Sequence[EffectRecord], unit_id: str) -> EffectRecord | None:
		"""Find the newest attempt for a unit that has no closing ledger fact."""
		attempts: dict[str, list[EffectRecord]] = {}
		for record in records:
			if record.unit_id == unit_id:
				attempts.setdefault(record.attempt_id, []).append(record)

		terminal_statuses = {EffectRecordStatus.COMMITTED, EffectRecordStatus.NOT_APPLIED}
		unresolved_statuses = {
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.UNKNOWN,
		}
		for attempt_records in sorted(attempts.values(), key=lambda items: max(item.seq for item in items), reverse=True):
			statuses = {record.status for record in attempt_records}
			if statuses.isdisjoint(terminal_statuses) and not statuses.isdisjoint(unresolved_statuses):
				return max(attempt_records, key=lambda record: record.seq)
		return None


def _effect_record_status(outcome: VerificationOutcome) -> EffectRecordStatus:
	"""Map a verification outcome to the closing reconciliation fact."""
	if outcome is VerificationOutcome.VERIFIED:
		return EffectRecordStatus.COMMITTED
	if outcome is VerificationOutcome.REJECTED:
		return EffectRecordStatus.NOT_APPLIED
	return EffectRecordStatus.UNKNOWN
