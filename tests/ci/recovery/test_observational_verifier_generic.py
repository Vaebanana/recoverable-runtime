"""Evidence must be grounded in read-only observations before it can verify a unit."""

from __future__ import annotations

from browser_use.recovery.contracts import ConditionSpec
from browser_use.recovery.observational_verifier import BrowserObservationVerifier, ObservationEvidence, ObservationSnapshot
from browser_use.recovery.verification import VerificationOutcome
from tests.ci.recovery.test_browser_use_adapter import make_unit


def observable_unit():
	unit = make_unit('u1')
	return unit.model_copy(
		update={
			'postconditions': (
				ConditionSpec(description='application is submitted', expected_observation='submitted successfully'),
			)
		}
	)


class StaticObservation:
	def __init__(self) -> None:
		self.visited: list[str] = []

	async def observe(self, context: object) -> ObservationSnapshot:
		return ObservationSnapshot(url='https://example.test/history', text='Application job_123 submitted successfully')

	async def observe_at(self, context: object, url: str) -> ObservationSnapshot:
		self.visited.append(url)
		return await self.observe(context)


class StaticInterpreter:
	def __init__(self, quote: str) -> None:
		self.quote = quote

	async def extract(self, unit: object, snapshot: ObservationSnapshot) -> ObservationEvidence:
		return ObservationEvidence(record_quote=snapshot.text, target_quote='job_123', condition_quotes=(self.quote,))


async def test_browser_verification_requires_grounded_condition_quote() -> None:
	unit = observable_unit()
	verified = await BrowserObservationVerifier(StaticObservation(), StaticInterpreter('submitted successfully')).verify(
		unit, None
	)
	assert verified.outcome is VerificationOutcome.VERIFIED
	inconclusive = await BrowserObservationVerifier(StaticObservation(), StaticInterpreter('approved yesterday')).verify(
		unit, None
	)
	assert inconclusive.outcome is VerificationOutcome.INCONCLUSIVE


async def test_browser_verification_navigates_to_declared_observation_url() -> None:
	unit = observable_unit()
	unit = unit.model_copy(
		update={'verification': unit.verification.model_copy(update={'observation_url': 'https://example.test/history'})}
	)
	source = StaticObservation()
	result = await BrowserObservationVerifier(source, StaticInterpreter('submitted successfully')).verify(unit, None)
	assert result.outcome is VerificationOutcome.VERIFIED
	assert source.visited == ['https://example.test/history']


async def test_quote_from_negative_status_cannot_verify_positive_postcondition() -> None:
	unit = make_unit('u1').model_copy(
		update={
			'postconditions': (ConditionSpec(description='application is submitted', expected_observation='Status: SUBMITTED'),)
		}
	)

	class NegativeObservation:
		async def observe(self, context: object) -> ObservationSnapshot:
			return ObservationSnapshot(url='https://example.test/history', text='job_123 Status: NOT_SUBMITTED')

	result = await BrowserObservationVerifier(NegativeObservation(), StaticInterpreter('Status: NOT_SUBMITTED')).verify(
		unit, None
	)
	assert result.outcome is VerificationOutcome.INCONCLUSIVE


async def test_explicit_negative_observation_rejects_unapplied_effect() -> None:
	unit = make_unit('u1').model_copy(
		update={
			'postconditions': (
				ConditionSpec(
					description='application is submitted',
					expected_observation='Status: SUBMITTED',
					negative_observation='Status: NOT_SUBMITTED',
				),
			)
		}
	)

	class NegativeObservation:
		async def observe(self, context: object) -> ObservationSnapshot:
			return ObservationSnapshot(url='https://example.test/history', text='job_123 Status: NOT_SUBMITTED')

	class NegativeInterpreter:
		async def extract(self, unit: object, snapshot: ObservationSnapshot) -> ObservationEvidence:
			return ObservationEvidence(
				record_quote=snapshot.text,
				target_quote='job_123',
				condition_quotes=('',),
				negative_quote='Status: NOT_SUBMITTED',
			)

	result = await BrowserObservationVerifier(NegativeObservation(), NegativeInterpreter()).verify(unit, None)
	assert result.outcome is VerificationOutcome.REJECTED


async def test_status_from_another_record_cannot_verify_or_reject_target() -> None:
	unit = observable_unit()
	page = 'job_123 Status: PENDING\njob_456 submitted successfully'

	class MultiRecordObservation:
		async def observe(self, context: object) -> ObservationSnapshot:
			return ObservationSnapshot(url='https://example.test/history', text=page)

	class CrossRecordInterpreter:
		async def extract(self, unit: object, snapshot: ObservationSnapshot) -> ObservationEvidence:
			return ObservationEvidence(
				record_quote='job_123 Status: PENDING',
				target_quote='job_123',
				condition_quotes=('submitted successfully',),
			)

	result = await BrowserObservationVerifier(MultiRecordObservation(), CrossRecordInterpreter()).verify(unit, None)
	assert result.outcome is VerificationOutcome.INCONCLUSIVE
