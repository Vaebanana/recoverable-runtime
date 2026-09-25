"""Generic verification through deliberately read-only observation capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from pydantic import Field

from browser_use.llm.base import BaseChatModel
from browser_use.llm.messages import UserMessage
from browser_use.recovery.contracts import FrozenModel, SemanticUnit, VerificationSource
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)


class ObservationSnapshot(FrozenModel):
	"""Current read-only page or external-tool observation."""

	url: str
	text: str


class ObservationEvidence(FrozenModel):
	"""Quoted text grounding each claimed postcondition in the observation."""

	record_quote: str = ''
	target_quote: str
	condition_quotes: tuple[str, ...] = ()
	negative_quote: str | None = None
	observed_fields: dict[str, str] = Field(default_factory=dict)


class ObservationSource(Protocol):
	"""Read a snapshot without exposing browser interaction methods."""

	async def observe(self, context: object) -> ObservationSnapshot:
		"""Return a fresh observation."""
		...


class ObservationInterpreter(Protocol):
	"""Extract quotes and structured facts without deciding runtime state."""

	async def extract(self, unit: SemanticUnit, snapshot: ObservationSnapshot) -> ObservationEvidence:
		"""Extract evidence from one snapshot."""
		...


class BrowserObservationSource:
	"""Expose only Browser Use's current URL and DOM snapshot."""

	async def observe(self, context: object) -> ObservationSnapshot:
		"""Read the active browser page without invoking actions or tools."""
		browser_session = getattr(context, 'browser_session', None)
		if browser_session is None:
			raise RuntimeError('no browser session is available for read-only verification')
		state = await browser_session.get_browser_state_summary(include_screenshot=False)
		return ObservationSnapshot(url=state.url, text=state.dom_state.llm_representation())

	async def observe_at(self, context: object, url: str) -> ObservationSnapshot:
		"""Navigate to a Contract-declared observation URL, then read the DOM."""
		browser_session = getattr(context, 'browser_session', None)
		if browser_session is None:
			raise RuntimeError('no browser session is available for observation navigation')
		await browser_session.start()
		await browser_session.navigate_to(url)
		return await self.observe(context)


class LLMObservationInterpreter:
	"""Ask an LLM to quote evidence; the verifier grounds every quote in the page."""

	def __init__(self, llm: BaseChatModel) -> None:
		self._llm = llm

	async def extract(self, unit: SemanticUnit, snapshot: ObservationSnapshot) -> ObservationEvidence:
		"""Return structured excerpts from the supplied snapshot only."""
		message = UserMessage(
			content=(
				'Extract verbatim evidence from this read-only page observation. '
				'Use one single-line record_quote containing the target and its status; '
				'never combine evidence from different records or DOM lines. '
				'Do not infer missing facts. Return one positive condition quote per postcondition, '
				'in order, and a negative quote only when the declared negative observation is visible. '
				'Use empty strings where no quote supports a condition.\n'
				f'Target: {unit.target.model_dump_json()}\n'
				f'Postconditions: {[condition.model_dump() for condition in unit.postconditions]}\n'
				f'Procedure: {unit.verification.procedure}\n'
				f'URL: {snapshot.url}\nPage:\n{snapshot.text[:40000]}'
			)
		)
		response = await self._llm.ainvoke([message], output_format=ObservationEvidence)
		return ObservationEvidence.model_validate(response.completion)


class BrowserObservationVerifier:
	"""Ground model-extracted evidence in a read-only browser snapshot."""

	observational = True

	def __init__(self, source: ObservationSource, interpreter: ObservationInterpreter) -> None:
		self._source = source
		self._interpreter = interpreter

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		"""Verify exact target identity and every quoted postcondition."""
		expected = expected_target_evidence(unit)
		try:
			if unit.verification.observation_url is not None:
				observe_at = getattr(self._source, 'observe_at', None)
				if not callable(observe_at):
					raise RuntimeError('observation source cannot navigate to the declared URL')
				snapshot = await cast(Callable[[object, str], Awaitable[ObservationSnapshot]], observe_at)(
					context, unit.verification.observation_url
				)
			else:
				snapshot = await self._source.observe(context)
			claims = await self._interpreter.extract(unit, snapshot)
		except Exception as exc:
			return self._result(unit, VerificationOutcome.INCONCLUSIVE, {}, f'observation failed: {exc}')

		text = snapshot.text.casefold()

		record = claims.record_quote.strip()
		record_grounded = (
			bool(record)
			and '\n' not in record
			and any(record.casefold() == line.strip().casefold() for line in snapshot.text.splitlines())
		)
		record_text = record.casefold() if record_grounded else ''
		identity_values = tuple(unit.target.attributes.values()) or (unit.target.key,)
		identity_grounded = (
			record_grounded
			and bool(claims.target_quote.strip())
			and claims.target_quote.casefold() in record_text
			and all(value.casefold() in claims.target_quote.casefold() for value in identity_values)
		)
		conditions_grounded = len(claims.condition_quotes) == len(unit.postconditions) and all(
			bool(quote.strip())
			and quote.casefold() in record_text
			and condition.expected_observation is not None
			and condition.expected_observation.casefold() in quote.casefold()
			for condition, quote in zip(unit.postconditions, claims.condition_quotes)
		)
		observed = {key: value for key, value in claims.observed_fields.items() if value.casefold() in text}
		observed['observation.url'] = snapshot.url
		if identity_grounded:
			observed.update(expected)
		if identity_grounded and conditions_grounded:
			for index, quote in enumerate(claims.condition_quotes):
				observed[f'postcondition.{index}.quote'] = quote
			return self._result(unit, VerificationOutcome.VERIFIED, observed, 'all postconditions have grounded evidence')
		if identity_grounded and claims.negative_quote and claims.negative_quote.casefold() in record_text:
			if any(
				condition.negative_observation is not None
				and condition.negative_observation.casefold() in claims.negative_quote.casefold()
				for condition in unit.postconditions
			):
				observed['negative.quote'] = claims.negative_quote
				return self._result(unit, VerificationOutcome.REJECTED, observed, 'explicit negative postcondition observed')
		return self._result(unit, VerificationOutcome.INCONCLUSIVE, observed, 'target or postcondition evidence is insufficient')

	@staticmethod
	def _result(unit: SemanticUnit, outcome: VerificationOutcome, observed: dict[str, str], summary: str) -> VerificationResult:
		"""Build a result for the existing verification manager to adjudicate."""
		return VerificationResult(
			outcome=outcome,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed=observed,
				source=unit.verification.source,
				summary=summary,
			),
		)


class ObservationalVerifier:
	"""Route a semantic unit to a supported read-only observation source."""

	observational = True

	def __init__(
		self,
		browser: BrowserObservationVerifier,
		external: BrowserObservationVerifier | None = None,
	) -> None:
		self._browser = browser
		self._external = external

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		"""Return INCONCLUSIVE if the declared observation source is unavailable."""
		if unit.verification.source is VerificationSource.BROWSER:
			return await self._browser.verify(unit, context)
		if unit.verification.source is VerificationSource.EXTERNAL_TOOL and self._external is not None:
			return await self._external.verify(unit, context)
		return BrowserObservationVerifier._result(unit, VerificationOutcome.INCONCLUSIVE, {}, 'observation source unavailable')
