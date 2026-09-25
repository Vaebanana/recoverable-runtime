"""Sidecar integration with Browser Use's existing step-hook boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from browser_use.llm.messages import UserMessage
from browser_use.recovery.contracts import FrozenModel, SemanticContract, SemanticUnit, UnitRuntimeState, UnitStatus
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.semantic_context import SemanticContextBuilder, SemanticRuntimeContext
from browser_use.recovery.verification import VerificationManager, VerificationResult, Verifier

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


class BrowserUseAdapterError(RuntimeError):
	"""Base error for Browser Use sidecar integration failures."""


class BrowserUseCompatibilityError(BrowserUseAdapterError):
	"""Raised when the isolated Browser Use message seam has changed."""


class RuntimeProgressBlockedError(BrowserUseAdapterError):
	"""Raised when UNKNOWN state forbids ordinary planner progress."""


class UnitSelectionRequiredError(BrowserUseAdapterError):
	"""Raised when multiple ready units require an explicit planner choice."""


class CompletionClaimError(BrowserUseAdapterError):
	"""Raised when a structured completion claim targets the wrong unit."""


class CompletionClaim(FrozenModel):
	"""Structured model assertion that one active unit appears complete."""

	candidate_unit_id: str
	claimed_complete: bool


class CompletionClaimSource(Protocol):
	"""Extract a structured claim without guessing from natural-language prose."""

	async def completion_claim(self, agent: Agent, unit: SemanticUnit) -> CompletionClaim | None:
		"""Return an explicit claim emitted for the active unit, if present."""
		...


class SemanticContextSink(Protocol):
	"""Publish one typed semantic context to a Browser Use Agent."""

	async def publish(self, agent: Agent, context: SemanticRuntimeContext) -> None:
		"""Make semantic context available before the next model decision."""
		...


class BrowserUseMessageContextSink:
	"""Isolate Browser Use's current private context-message compatibility seam."""

	async def publish(self, agent: Agent, context: SemanticRuntimeContext) -> None:
		"""Append context through the same private helper used by Agent core."""
		message_manager = getattr(agent, 'message_manager', None)
		add_context_message = getattr(message_manager, '_add_context_message', None)
		if not callable(add_context_message):
			raise BrowserUseCompatibilityError('Browser Use message manager no longer exposes callable _add_context_message')
		add_context_message(UserMessage(content=context.render()))


class BrowserUseStepRecord(FrozenModel):
	"""Compact serializable trace of one observed Browser Use step."""

	step_number: int
	active_unit_id: str | None = None
	model_output_present: bool
	action_count: int
	result_count: int
	result_errors: tuple[str, ...] = ()
	completion_claim: CompletionClaim | None = None
	verification_result: VerificationResult | None = None


class BrowserUseRuntimeAdapter:
	"""Own semantic Runtime state around Browser Use's native step loop."""

	def __init__(
		self,
		*,
		contract: SemanticContract,
		verifier: Verifier[Agent],
		claim_source: CompletionClaimSource,
		states: dict[str, UnitRuntimeState] | None = None,
		context_sink: SemanticContextSink | None = None,
		state_manager: RuntimeStateManager | None = None,
		context_builder: SemanticContextBuilder | None = None,
		protect_side_effect_completion: bool = False,
	) -> None:
		self._contract = contract
		self._state_manager = state_manager or RuntimeStateManager()
		self._states = dict(states) if states is not None else self._state_manager.initialize(contract)
		self._verifier = verifier
		self._verification_manager: VerificationManager[Agent] = VerificationManager(self._state_manager)
		self._claim_source = claim_source
		self._context_sink = context_sink or BrowserUseMessageContextSink()
		self._context_builder = context_builder or SemanticContextBuilder()
		self._protect_side_effect_completion = protect_side_effect_completion
		self._trace: list[BrowserUseStepRecord] = []
		self._last_context: SemanticRuntimeContext | None = None
		self._context_builder.build(self._contract, self._states)

	@property
	def contract(self) -> SemanticContract:
		"""Return the immutable Contract snapshot used by the adapter."""
		return self._contract

	@property
	def states(self) -> dict[str, UnitRuntimeState]:
		"""Return a shallow copy of the immutable per-unit state snapshot."""
		return dict(self._states)

	@property
	def trace(self) -> tuple[BrowserUseStepRecord, ...]:
		"""Return the accumulated compact step records."""
		return tuple(self._trace)

	@property
	def last_context(self) -> SemanticRuntimeContext | None:
		"""Return the most recently published semantic context."""
		return self._last_context

	@property
	def context_builder(self) -> SemanticContextBuilder:
		"""Expose the stateless builder for integrations that need preview context."""
		return self._context_builder

	def activate(self, unit_id: str) -> None:
		"""Apply an explicit planner selection through Runtime validation."""
		context = self._context_builder.build(self._contract, self._states)
		if context.progress_blocked:
			raise RuntimeProgressBlockedError(
				f'normal progress is blocked by UNKNOWN units: {", ".join(context.blocked_unit_ids)}'
			)
		self._states = self._state_manager.activate(self._contract, self._states, unit_id)

	def apply_runtime_states(self, states: Mapping[str, UnitRuntimeState]) -> None:
		"""Replace the Runtime snapshot after validating it through the context builder.

		Used by the action-level side-effect bridge to write the authoritative
		post-execution snapshot returned by ``SideEffectCoordinator.execute`` back
		into the adapter, keeping SQLite, the adapter, and the bridge's
		``last_effect_seq`` consistent after one action.
		"""
		next_states = dict(states)
		self._context_builder.build(self._contract, next_states)
		self._states = next_states

	async def on_step_start(self, agent: Agent) -> None:
		"""Validate progress, choose only an unambiguous unit, and publish context."""
		context = self._context_builder.build(self._contract, self._states)
		if context.progress_blocked:
			await self._publish_context(agent, context)
			raise RuntimeProgressBlockedError(
				f'normal progress is blocked by UNKNOWN units: {", ".join(context.blocked_unit_ids)}'
			)

		if context.active_unit is None and len(context.ready_unit_ids) > 1:
			await self._publish_context(agent, context)
			raise UnitSelectionRequiredError(
				f'explicit unit selection required among ready units: {", ".join(context.ready_unit_ids)}'
			)
		if context.active_unit is None and len(context.ready_unit_ids) == 1:
			self._states = self._state_manager.activate(
				self._contract,
				self._states,
				context.ready_unit_ids[0],
			)
			context = self._context_builder.build(self._contract, self._states)

		await self._publish_context(agent, context)

	async def on_step_end(self, agent: Agent) -> None:
		"""Record the step and process only an explicit structured completion claim."""
		active_unit = self._active_unit()
		claim = await self._claim_source.completion_claim(agent, active_unit) if active_unit is not None else None
		if claim is None or not claim.claimed_complete:
			self._trace.append(self._step_record(agent, active_unit, claim, None))
			return
		if active_unit is None or claim.candidate_unit_id != active_unit.unit_id:
			self._trace.append(self._step_record(agent, active_unit, claim, None))
			active_unit_id = active_unit.unit_id if active_unit is not None else 'none'
			raise CompletionClaimError(
				f'completion claim for {claim.candidate_unit_id} does not match active unit {active_unit_id}'
			)
		if active_unit.effect.has_side_effect and self._protect_side_effect_completion:
			self._trace.append(self._step_record(agent, active_unit, claim, None))
			raise CompletionClaimError(f'{active_unit.unit_id}: a plain completion claim cannot bypass the effect boundary')

		candidate_states = self._state_manager.claim_completion(
			self._contract,
			self._states,
			active_unit.unit_id,
		)
		updated_states, result = await self._verification_manager.verify_candidate(
			self._contract,
			candidate_states,
			active_unit.unit_id,
			self._verifier,
			agent,
		)
		self._states = updated_states
		self._trace.append(self._step_record(agent, active_unit, claim, result))

	async def _publish_context(self, agent: Agent, context: SemanticRuntimeContext) -> None:
		"""Publish and retain one context snapshot."""
		await self._context_sink.publish(agent, context)
		self._last_context = context

	def _active_unit(self) -> SemanticUnit | None:
		"""Return the sole active semantic unit, if one exists."""
		for unit_id, state in self._states.items():
			if state.status is UnitStatus.ACTIVE:
				return self._contract.get_unit(unit_id)
		return None

	@staticmethod
	def _step_record(
		agent: Agent,
		active_unit: SemanticUnit | None,
		claim: CompletionClaim | None,
		result: VerificationResult | None,
	) -> BrowserUseStepRecord:
		"""Project Browser Use state into a small serializable trace record."""
		last_model_output = agent.state.last_model_output
		actions = getattr(last_model_output, 'action', None) if last_model_output is not None else None
		last_results = agent.state.last_result or []
		return BrowserUseStepRecord(
			step_number=agent.state.n_steps,
			active_unit_id=active_unit.unit_id if active_unit is not None else None,
			model_output_present=last_model_output is not None,
			action_count=len(actions) if actions is not None else 0,
			result_count=len(last_results),
			result_errors=tuple(result.error for result in last_results if result.error is not None),
			completion_claim=claim,
			verification_result=result,
		)
