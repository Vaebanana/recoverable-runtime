"""Real browser verifier for the recovery smoke application page.

The verifier observes the application status through the live browser page
(never through in-process state) and returns the authoritative outcome used by
``SideEffectCoordinator``:

    Status: SUBMITTED     -> VERIFIED
    Status: NOT_SUBMITTED -> REJECTED
    unreadable/unknown    -> INCONCLUSIVE
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from browser_use.recovery.contracts import SemanticContract, SemanticUnit
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

StatusReader = Callable[[], Awaitable[dict[str, str]]]


def build_page_status_reader(browser_session: BrowserSession) -> StatusReader:
	"""Return a reader that fetches /status JSON through the live browser page."""

	async def read() -> dict[str, str]:
		page = await browser_session.must_get_current_page()
		raw = await page.evaluate("() => fetch('/status').then((r) => r.json())")
		payload = json.loads(raw)
		return {
			# The bare attribute key is required so VERIFIED evidence identifies
			# the exact Contract target identity.
			'application_id': str(payload.get('application_id', '')),
			'application.status': str(payload.get('status', '')),
			'submit.count': str(payload.get('submit_count', '')),
		}

	return read


class ApplicationStatusVerifier:
	"""Judge the application submission by observing the external status page."""

	observational = True

	def __init__(self, contract: SemanticContract, status_reader: StatusReader) -> None:
		self._contract = contract
		self._status_reader = status_reader

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		"""Observe the status page and map it to a verification outcome."""
		try:
			observed = dict(await self._status_reader())
		except Exception as exc:
			return VerificationResult(
				outcome=VerificationOutcome.INCONCLUSIVE,
				evidence=VerificationEvidence(
					expected=expected_target_evidence(unit),
					observed={},
					source=unit.verification.source,
					summary='application status page could not be observed',
				),
				detail=f'{type(exc).__name__}: {exc}',
			)

		status = observed.get('application.status')
		if status == 'SUBMITTED':
			outcome = VerificationOutcome.VERIFIED
			summary = 'application status page shows SUBMITTED'
		elif status == 'NOT_SUBMITTED':
			outcome = VerificationOutcome.REJECTED
			summary = 'application status page still shows NOT_SUBMITTED'
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
