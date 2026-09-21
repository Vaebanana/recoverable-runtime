from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_use.recovery.contracts import (
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
from browser_use.recovery.verification import VerificationOutcome, expected_target_evidence
from experiments.recovery_smoke import server
from experiments.recovery_smoke.verifier import ApplicationStatusVerifier


def application_contract() -> SemanticContract:
	"""Build the smoke application Contract."""
	unit = SemanticUnit(
		unit_id='u_submit',
		identity=UnitIdentity(
			intent_key='submit_application',
			target_key='application/7',
			outcome_key='application_submitted',
		),
		intent='submit application #7',
		target=TargetSpec(type='application', key='application/7', attributes={'application_id': '7'}),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
		),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='observe the application status page',
		),
	)
	return SemanticContract(contract_id='c1', task_id='task-1', version=1, units=(unit,))


class StaticStatusReader:
	"""Return one configured status observation."""

	def __init__(self, payload: dict[str, str] | None, error: Exception | None = None) -> None:
		self._payload = payload
		self._error = error

	async def __call__(self) -> dict[str, str]:
		if self._error is not None:
			raise self._error
		if self._payload is None:
			raise RuntimeError('no observation available')
		return self._payload


@pytest.mark.asyncio
async def test_verifier_maps_page_status_to_outcomes() -> None:
	contract = application_contract()
	unit = contract.get_unit('u_submit')

	verified = await ApplicationStatusVerifier(contract, StaticStatusReader({'application.status': 'SUBMITTED'})).verify(
		unit, None
	)
	assert verified.outcome is VerificationOutcome.VERIFIED
	assert verified.evidence.expected == expected_target_evidence(unit)
	assert verified.evidence.source is VerificationSource.BROWSER
	assert verified.evidence.observed['application.status'] == 'SUBMITTED'

	rejected = await ApplicationStatusVerifier(contract, StaticStatusReader({'application.status': 'NOT_SUBMITTED'})).verify(
		unit, None
	)
	assert rejected.outcome is VerificationOutcome.REJECTED

	inconclusive = await ApplicationStatusVerifier(contract, StaticStatusReader({'application.status': 'LOADING'})).verify(
		unit, None
	)
	assert inconclusive.outcome is VerificationOutcome.INCONCLUSIVE

	unreadable = await ApplicationStatusVerifier(contract, StaticStatusReader(None, error=RuntimeError('page crashed'))).verify(
		unit, None
	)
	assert unreadable.outcome is VerificationOutcome.INCONCLUSIVE
	assert unreadable.detail.startswith('RuntimeError')


def test_server_keeps_external_state_in_sqlite(tmp_path: Path) -> None:
	server._db_path = tmp_path / 'state.db'

	with TestClient(server.app) as client:
		page = client.get('/')
		assert 'Status: NOT_SUBMITTED' in page.text
		assert 'Submit count: 0' in page.text

		submitted_page = client.post('/submit')
		assert 'Status: SUBMITTED' in submitted_page.text
		assert 'Submit count: 1' in submitted_page.text

		assert client.get('/status').json() == {
			'application_id': '7',
			'status': 'SUBMITTED',
			'submit_count': 1,
		}

		# A second submission is still recorded; the harness is responsible for
		# ensuring exactly one submit, the server just reports the facts.
		client.post('/submit')
		assert client.get('/status').json()['submit_count'] == 2

	# A fresh server process over the same state file remembers the world.
	with TestClient(server.app) as restarted_client:
		assert restarted_client.get('/status').json() == {
			'application_id': '7',
			'status': 'SUBMITTED',
			'submit_count': 2,
		}
