"""Evidence-bearing completion verification for semantic units."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from pydantic import Field, field_serializer, field_validator

from browser_use.recovery.contracts import FrozenModel, SemanticContract, SemanticUnit, UnitRuntimeState, VerificationSource

if TYPE_CHECKING:
	from browser_use.recovery.runtime_state import RuntimeStateManager


def _empty_string_mapping() -> Mapping[str, str]:
	"""Create an immutable empty mapping for evidence defaults."""
	return MappingProxyType({})


class VerificationOutcome(StrEnum):
	"""Authoritative result of postcondition verification."""

	VERIFIED = 'verified'
	REJECTED = 'rejected'
	INCONCLUSIVE = 'inconclusive'


class VerificationEvidence(FrozenModel):
	"""Structured expected identity and runtime observations."""

	expected: Mapping[str, str] = Field(default_factory=_empty_string_mapping)
	observed: Mapping[str, str] = Field(default_factory=_empty_string_mapping)
	source: VerificationSource
	summary: str

	@field_validator('expected', 'observed', mode='after')
	@classmethod
	def freeze_mapping(cls, value: Mapping[str, str]) -> Mapping[str, str]:
		"""Copy caller-owned evidence into an immutable mapping."""
		return MappingProxyType(dict(value))

	@field_serializer('expected', 'observed')
	def serialize_mapping(self, value: Mapping[str, str]) -> dict[str, str]:
		"""Serialize immutable evidence through a plain dictionary schema."""
		return dict(value)


class VerificationResult(FrozenModel):
	"""Auditable outcome returned by an observational verifier."""

	outcome: VerificationOutcome
	evidence: VerificationEvidence
	observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
	detail: str = ''

	@field_validator('observed_at')
	@classmethod
	def require_timezone(cls, value: datetime) -> datetime:
		"""Require verification timestamps to identify an absolute instant."""
		if value.tzinfo is None or value.utcoffset() is None:
			raise ValueError('observed_at must be timezone-aware')
		return value


class VerificationError(ValueError):
	"""Raised when a verifier violates the verification protocol."""


VerificationContextT = TypeVar('VerificationContextT', contravariant=True)


class Verifier(Protocol[VerificationContextT]):
	"""Read-only evidence collector and deterministic postcondition judge."""

	observational: bool

	async def verify(self, unit: SemanticUnit, context: VerificationContextT) -> VerificationResult:
		"""Observe the world and return structured evidence plus an outcome."""
		...


def expected_target_evidence(unit: SemanticUnit) -> dict[str, str]:
	"""Project stable Contract target identity into verification fields."""
	return {
		'target.type': unit.target.type,
		'target.key': unit.target.key,
		**{f'target.attributes.{key}': value for key, value in sorted(unit.target.attributes.items())},
	}


VerificationManagerContextT = TypeVar('VerificationManagerContextT')


class VerificationManager(Generic[VerificationManagerContextT]):
	"""Orchestrate verification while retaining Runtime state authority."""

	def __init__(self, state_manager: RuntimeStateManager | None = None) -> None:
		if state_manager is None:
			from browser_use.recovery.runtime_state import RuntimeStateManager

			state_manager = RuntimeStateManager()
		self._state_manager = state_manager

	async def verify_candidate(
		self,
		contract: SemanticContract,
		states: dict[str, UnitRuntimeState],
		unit_id: str,
		verifier: Verifier[VerificationManagerContextT],
		context: VerificationManagerContextT,
	) -> tuple[dict[str, UnitRuntimeState], VerificationResult]:
		"""Verify one candidate and atomically return its next state snapshot."""
		if verifier.observational is not True:
			raise VerificationError('verifier must declare observational=True')

		unit = contract.get_unit(unit_id)
		verifying_states = self._state_manager.begin_verification(contract, states, unit_id)
		try:
			result = await verifier.verify(unit, context)
		except Exception as exc:
			result = self._inconclusive_from_error(unit, exc)
		else:
			result = self._validate_evidence_identity(unit, result)

		updated = self._state_manager.apply_verification_result(contract, verifying_states, unit_id, result)
		return updated, result

	@staticmethod
	def _validate_evidence_identity(unit: SemanticUnit, result: VerificationResult) -> VerificationResult:
		"""Refuse outcomes whose expected identity is not the Contract target."""
		expected = expected_target_evidence(unit)
		if result.evidence.expected != expected or result.evidence.source is not unit.verification.source:
			return VerificationResult(
				outcome=VerificationOutcome.INCONCLUSIVE,
				evidence=VerificationEvidence(
					expected=expected,
					observed=result.evidence.observed,
					source=unit.verification.source,
					summary='verifier evidence did not match the Contract verification identity',
				),
				observed_at=result.observed_at,
				detail='verifier returned mismatched expected fields or evidence source',
			)
		if result.outcome is VerificationOutcome.VERIFIED and not _observed_target_matches(unit, result.evidence.observed):
			return VerificationResult(
				outcome=VerificationOutcome.INCONCLUSIVE,
				evidence=result.evidence,
				observed_at=result.observed_at,
				detail='VERIFIED evidence does not contain the Contract observed target identity',
			)
		return result

	@staticmethod
	def _inconclusive_from_error(unit: SemanticUnit, error: Exception) -> VerificationResult:
		"""Preserve verifier failure as uncertainty rather than retry permission."""
		return VerificationResult(
			outcome=VerificationOutcome.INCONCLUSIVE,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed={},
				source=unit.verification.source,
				summary='verification could not obtain conclusive evidence',
			),
			detail=f'{type(error).__name__}: {error}',
		)


def _observed_target_matches(unit: SemanticUnit, observed: Mapping[str, str]) -> bool:
	"""Require VERIFIED evidence to identify the exact Contract target."""
	if unit.target.attributes:
		for key, expected_value in unit.target.attributes.items():
			observed_value = observed.get(f'target.attributes.{key}', observed.get(key))
			if observed_value != expected_value:
				return False
	else:
		observed_key = observed.get('target.key', observed.get('key'))
		if observed_key != unit.target.key:
			return False

	explicit_type = observed.get('target.type')
	if explicit_type is not None and explicit_type != unit.target.type:
		return False
	explicit_key = observed.get('target.key')
	return explicit_key is None or explicit_key == unit.target.key
