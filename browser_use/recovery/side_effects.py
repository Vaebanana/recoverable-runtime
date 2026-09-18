"""Single durable execution boundary for external side effects."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from uuid import uuid4

from pydantic import field_serializer, field_validator

from browser_use.recovery.contracts import FrozenModel, SemanticContract, UnitRuntimeState
from browser_use.recovery.persistence.checkpoint import CheckpointManager
from browser_use.recovery.persistence.models import (
	EffectRecord,
	EffectRecordDraft,
	EffectRecordStatus,
)
from browser_use.recovery.persistence.storage import RuntimeStorage
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationManager,
	VerificationOutcome,
	VerificationResult,
	Verifier,
	expected_target_evidence,
)


class SideEffectExecutionError(ValueError):
	"""Raised when a unit cannot enter the durable side-effect boundary."""


class SideEffectExecutionResult(FrozenModel):
	"""Structured durable outcome of one external action attempt."""

	states: Mapping[str, UnitRuntimeState]
	records: tuple[EffectRecord, ...]
	verification_result: VerificationResult
	action_succeeded: bool
	action_error: str | None = None

	@field_validator('states', mode='after')
	@classmethod
	def freeze_states(cls, value: Mapping[str, UnitRuntimeState]) -> Mapping[str, UnitRuntimeState]:
		"""Copy the returned Runtime snapshot into immutable storage."""
		return MappingProxyType(dict(value))

	@field_serializer('states')
	def serialize_states(self, value: Mapping[str, UnitRuntimeState]) -> dict[str, UnitRuntimeState]:
		"""Serialize immutable states through a regular mapping."""
		return dict(value)


class SideEffectCoordinator:
	"""Persist intent, execute once, verify, and atomically close the state transition."""

	def __init__(
		self,
		*,
		storage: RuntimeStorage,
		checkpoint_manager: CheckpointManager,
		workflow_id: str,
		run_id: str,
		contract: SemanticContract,
		verifier: Verifier[object],
		state_manager: RuntimeStateManager | None = None,
		effect_id_factory: Callable[[], str] | None = None,
		attempt_id_factory: Callable[[], str] | None = None,
	) -> None:
		self._storage = storage
		self._checkpoint_manager = checkpoint_manager
		self._workflow_id = workflow_id
		self._run_id = run_id
		self._contract = contract
		self._verifier = verifier
		self._state_manager = state_manager or RuntimeStateManager()
		self._verification_manager: VerificationManager[object] = VerificationManager(self._state_manager)
		self._effect_id_factory = effect_id_factory or (lambda: f'effect_{uuid4().hex}')
		self._attempt_id_factory = attempt_id_factory or (lambda: f'attempt_{uuid4().hex}')

	async def execute(
		self,
		*,
		unit_id: str,
		effect_key: str,
		states: dict[str, UnitRuntimeState],
		last_effect_seq: int,
		executor: Callable[[], Awaitable[object]],
		idempotency_key: str | None = None,
	) -> SideEffectExecutionResult:
		"""Execute exactly once and persist verification as the authoritative outcome."""
		unit = self._contract.get_unit(unit_id)
		if not unit.effect.has_side_effect:
			raise SideEffectExecutionError(f'unit {unit_id} has no external side effect')
		if unit_id not in states:
			raise SideEffectExecutionError(f'missing Runtime state for unit {unit_id}')

		effect_id = self._effect_id_factory()
		attempt_id = self._attempt_id_factory()
		common_fields: dict[str, object] = {
			'effect_id': effect_id,
			'workflow_id': self._workflow_id,
			'run_id': self._run_id,
			'unit_id': unit_id,
			'effect_key': effect_key,
			'attempt_id': attempt_id,
			'idempotency_key': idempotency_key,
		}

		prepared_checkpoint = self._checkpoint_manager.build(
			workflow_id=self._workflow_id,
			run_id=self._run_id,
			contract=self._contract,
			states=states,
			last_effect_seq=last_effect_seq,
			metadata={'effect_phase': EffectRecordStatus.PREPARED.value, 'attempt_id': attempt_id},
		)
		prepared, prepared_checkpoint = self._storage.commit_effect_and_checkpoint(
			EffectRecordDraft.model_validate({**common_fields, 'status': EffectRecordStatus.PREPARED}),
			prepared_checkpoint,
		)

		action_succeeded = False
		action_error: str | None = None
		try:
			action_result = await executor()
		except Exception as exc:
			action_error = f'{type(exc).__name__}: {exc}'
			attempted = self._storage.append_effect(
				EffectRecordDraft.model_validate(
					{
						**common_fields,
						'status': EffectRecordStatus.ATTEMPTED,
						'metadata': {'action_error': action_error},
					}
				)
			)
			verification_result = VerificationResult(
				outcome=VerificationOutcome.INCONCLUSIVE,
				evidence=VerificationEvidence(
					expected=expected_target_evidence(unit),
					observed={},
					source=unit.verification.source,
					summary='action raised after the durable PREPARED boundary',
				),
				detail=action_error,
			)
			candidate_states = self._state_manager.claim_completion(self._contract, states, unit_id)
			verifying_states = self._state_manager.begin_verification(self._contract, candidate_states, unit_id)
			updated_states = self._state_manager.apply_verification_result(
				self._contract,
				verifying_states,
				unit_id,
				verification_result,
			)
		else:
			action_succeeded = True
			attempted = self._storage.append_effect(
				EffectRecordDraft.model_validate(
					{
						**common_fields,
						'status': EffectRecordStatus.ATTEMPTED,
						'metadata': {'action_succeeded': True},
					}
				)
			)
			candidate_states = self._state_manager.claim_completion(self._contract, states, unit_id)
			updated_states, verification_result = await self._verification_manager.verify_candidate(
				self._contract,
				candidate_states,
				unit_id,
				self._verifier,
				action_result,
			)

		final_status = _effect_record_status(verification_result.outcome)
		final_checkpoint = self._checkpoint_manager.build(
			workflow_id=self._workflow_id,
			run_id=self._run_id,
			contract=self._contract,
			states=updated_states,
			last_effect_seq=attempted.seq,
			metadata={'effect_phase': final_status.value, 'attempt_id': attempt_id},
		)
		final_record, _ = self._storage.commit_effect_and_checkpoint(
			EffectRecordDraft.model_validate(
				{
					**common_fields,
					'status': final_status,
					'evidence': verification_result.model_dump(mode='json'),
					'metadata': {'action_error': action_error} if action_error is not None else {},
				}
			),
			final_checkpoint,
		)
		return SideEffectExecutionResult(
			states=updated_states,
			records=(prepared, attempted, final_record),
			verification_result=verification_result,
			action_succeeded=action_succeeded,
			action_error=action_error,
		)


def _effect_record_status(outcome: VerificationOutcome) -> EffectRecordStatus:
	"""Map authoritative verification outcomes to immutable ledger facts."""
	if outcome is VerificationOutcome.VERIFIED:
		return EffectRecordStatus.COMMITTED
	if outcome is VerificationOutcome.REJECTED:
		return EffectRecordStatus.NOT_APPLIED
	return EffectRecordStatus.UNKNOWN
