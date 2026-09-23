"""Shared business scenario for Day 10 process-level recovery experiments."""

from __future__ import annotations

from enum import StrEnum

import httpx

from browser_use.recovery import (
	ConditionSpec,
	EffectSpec,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)

UNIT_ID = 'u_submit'
CONTRACT_ID = 'process-recovery-application'
APPLICATION_ID = '7'

EXIT_AFTER_PREPARED = 81
EXIT_AFTER_ATTEMPTED = 82


class ProcessScenario(StrEnum):
	"""Fault/recovery scenarios exercised across independent Python processes."""

	AFTER_PREPARED = 'after_prepared'
	AFTER_ATTEMPTED = 'after_attempted'
	VERIFIER_UNAVAILABLE = 'verifier_unavailable'


def build_contract() -> SemanticContract:
	"""Build the single-unit Contract persisted by the crash worker."""
	unit = SemanticUnit(
		unit_id=UNIT_ID,
		identity=UnitIdentity(
			intent_key='submit_application',
			target_key=f'application/{APPLICATION_ID}',
			outcome_key='application_submitted',
		),
		intent=f'submit application #{APPLICATION_ID}',
		target=TargetSpec(
			type='application',
			key=f'application/{APPLICATION_ID}',
			attributes={'application_id': APPLICATION_ID},
		),
		postconditions=(ConditionSpec(description=f'application #{APPLICATION_ID} has submitted status'),),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
			description='POST /submit on the controllable application server',
		),
		verification=VerificationSpec(
			source=VerificationSource.EXTERNAL_TOOL,
			procedure='GET /status and confirm the application status',
		),
	)
	return SemanticContract(
		contract_id=CONTRACT_ID,
		task_id=f'submit-application-{APPLICATION_ID}',
		version=1,
		units=(unit,),
	)


async def submit_application(server_url: str) -> dict[str, object]:
	"""Execute the real external side effect through the local application server."""
	async with httpx.AsyncClient() as client:
		response = await client.post(f'{server_url}/submit')
		response.raise_for_status()
	return {'submitted': True, 'application_id': APPLICATION_ID}


async def fetch_application_status(server_url: str) -> dict[str, object]:
	"""Read the external world without consulting Runtime persistence."""
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{server_url}/status')
		response.raise_for_status()
		return response.json()


class HttpApplicationStatusVerifier:
	"""Observe the application server and judge the Contract postcondition."""

	observational = True

	def __init__(self, server_url: str) -> None:
		self._server_url = server_url

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		try:
			payload = await fetch_application_status(self._server_url)
		except Exception as exc:
			return VerificationResult(
				outcome=VerificationOutcome.INCONCLUSIVE,
				evidence=VerificationEvidence(
					expected=expected_target_evidence(unit),
					observed={},
					source=unit.verification.source,
					summary='application status could not be observed',
				),
				detail=f'{type(exc).__name__}: {exc}',
			)

		observed = {
			'application_id': str(payload.get('application_id', '')),
			'application.status': str(payload.get('status', '')),
			'submit.count': str(payload.get('submit_count', '')),
		}
		status = observed['application.status']
		if status == 'SUBMITTED':
			outcome = VerificationOutcome.VERIFIED
			summary = 'application status is SUBMITTED'
		elif status == 'NOT_SUBMITTED':
			outcome = VerificationOutcome.REJECTED
			summary = 'application status is still NOT_SUBMITTED'
		else:
			outcome = VerificationOutcome.INCONCLUSIVE
			summary = f'application status is unrecognized: {status!r}'

		return VerificationResult(
			outcome=outcome,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed=observed,
				source=unit.verification.source,
				summary=summary,
			),
		)


class UnavailableVerifier:
	"""Model a temporary observation outage without changing external state."""

	observational = True

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		raise ConnectionError('injected verifier outage')
