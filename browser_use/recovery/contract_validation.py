"""Deterministic contract checks before a recoverable workflow begins."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from browser_use.recovery.contracts import Idempotency, Reversibility, SemanticContract, SemanticUnit, VerificationSource


class ContractPolicyError(ValueError):
	"""A contract cannot be executed safely by the generic harness."""


class EffectPolicyValidator:
	"""Conservatively classify obvious effect semantics in a proposed unit."""

	_EFFECT_VERBS = re.compile(
		r'\b(submit|send|create|purchase|delete|update|publish|confirm|post|pay)\b|提交|发送|创建|购买|删除|更新|发布|确认', re.I
	)

	def validate(self, unit: SemanticUnit) -> SemanticUnit:
		"""Upgrade an obvious effect contradiction without inspecting browser actions."""
		if unit.effect.has_side_effect or not self._EFFECT_VERBS.search(unit.intent):
			return unit
		return unit.model_copy(
			update={
				'effect': unit.effect.model_copy(
					update={
						'has_side_effect': True,
						'idempotency': Idempotency.UNKNOWN,
						'reversibility': Reversibility.UNKNOWN,
					}
				)
			}
		)


class VerificationPolicyValidator:
	"""Reject missing or contradictory observation semantics."""

	def validate(self, unit: SemanticUnit) -> None:
		"""Check structural evidence requirements; runtime capabilities enforce read-only access."""
		if not unit.target.type.strip() or not unit.target.key.strip():
			raise ContractPolicyError(f'{unit.unit_id}: a target type and key are required')
		if not unit.postconditions or any(not condition.description.strip() for condition in unit.postconditions):
			raise ContractPolicyError(f'{unit.unit_id}: observable postconditions are required')
		if any(
			not condition.expected_observation or not condition.expected_observation.strip() for condition in unit.postconditions
		):
			raise ContractPolicyError(f'{unit.unit_id}: each postcondition requires expected_observation')
		verification = unit.verification
		if unit.effect.has_side_effect and not verification.required:
			raise ContractPolicyError(f'{unit.unit_id}: side effects require verification')
		if verification.source is not VerificationSource.BROWSER:
			raise ContractPolicyError(f'{unit.unit_id}: {verification.source.name} is outside RecoverableHarness v1')
		if not verification.procedure.strip():
			raise ContractPolicyError(f'{unit.unit_id}: a concrete observation procedure is required')
		if verification.observation_url is not None:
			if not verification.observation_is_read_only:
				raise ContractPolicyError(f'{unit.unit_id}: observation_url requires an explicit read-only assertion')
			parsed = urlparse(verification.observation_url)
			if (
				verification.source is not VerificationSource.BROWSER
				or parsed.scheme not in {'http', 'https'}
				or not parsed.netloc
			):
				raise ContractPolicyError(f'{unit.unit_id}: observation_url must be a browser HTTP(S) URL')


def validate_contract(contract: SemanticContract) -> SemanticContract:
	"""Return a validated, conservatively upgraded immutable contract snapshot."""
	effect_policy = EffectPolicyValidator()
	verification_policy = VerificationPolicyValidator()
	units = tuple(effect_policy.validate(unit) for unit in contract.units)
	for unit in units:
		verification_policy.validate(unit)
	return contract.model_copy(update={'units': units})
