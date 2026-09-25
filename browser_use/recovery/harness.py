"""Unified recoverable sidecar around the native Browser Use Agent loop."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import MethodType
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field, create_model

from browser_use.agent.views import ActionResult, AgentHistoryList, AgentOutput, StepMetadata
from browser_use.llm.messages import UserMessage
from browser_use.recovery.bootstrap import RecoveryBootstrap
from browser_use.recovery.browser_use_adapter import (
	BrowserUseRuntimeAdapter,
	CompletionClaim,
	CompletionClaimError,
	CompletionClaimSource,
	SemanticContextSink,
)
from browser_use.recovery.contract_validation import ContractPolicyError, validate_contract
from browser_use.recovery.contracts import SemanticContract, SemanticUnit, UnitRuntimeState, UnitStatus, VerificationSource
from browser_use.recovery.effect_boundary import EffectBoundaryDeclaration, EffectBoundaryExecutor, EffectBoundaryPlan
from browser_use.recovery.observational_verifier import (
	BrowserObservationSource,
	BrowserObservationVerifier,
	LLMObservationInterpreter,
	ObservationalVerifier,
)
from browser_use.recovery.persistence.checkpoint import CheckpointManager
from browser_use.recovery.persistence.models import WorkflowRun
from browser_use.recovery.persistence.storage import RuntimeStorage
from browser_use.recovery.prompt_memory import UnitPromptMemoryPolicy
from browser_use.recovery.reconciliation import ReconciliationCoordinator
from browser_use.recovery.side_effects import SideEffectCoordinator
from browser_use.recovery.verification import VerificationManager, Verifier

if TYPE_CHECKING:
	from browser_use.agent.service import Agent
	from browser_use.agent.views import ActionModel
	from browser_use.browser.views import BrowserStateSummary
	from browser_use.recovery.semantic_context import SemanticRuntimeContext


class _DeferredContextSink(SemanticContextSink):
	"""Retain context until after Agent._prepare_context clears old messages."""

	def __init__(self) -> None:
		self.context: SemanticRuntimeContext | None = None

	async def publish(self, agent: Agent, context: SemanticRuntimeContext) -> None:
		"""Store the semantic context for the upcoming model call."""
		self.context = context


class _DoneClaimSource(CompletionClaimSource):
	"""Treat a native done action as a claim only for a non-effect unit."""

	async def completion_claim(self, agent: Agent, unit: SemanticUnit) -> CompletionClaim | None:
		"""Extract an explicit claim from the native action list."""
		output = agent.state.last_model_output
		if output is None:
			return None
		claimed_unit_id = getattr(output, 'completion_claim_unit_id', None)
		if claimed_unit_id is not None:
			return CompletionClaim(candidate_unit_id=claimed_unit_id, claimed_complete=True)
		for action in output.action:
			if action.model_dump(exclude_none=True).get('done') is not None:
				return CompletionClaim(candidate_unit_id=unit.unit_id, claimed_complete=True)
		return None


class RecoverableHarness:
	"""Manage semantic checkpoints, effect boundaries, history, and resume."""

	def __init__(
		self,
		*,
		agent: Agent,
		contract: SemanticContract,
		storage: RuntimeStorage,
		workflow_id: str,
		run_id: str | None = None,
		history_path: str | Path | None = None,
		verifier: Verifier[object] | None = None,
		states: dict[str, UnitRuntimeState] | None = None,
		last_effect_seq: int = 0,
		_start_run: bool = True,
	) -> None:
		self.agent = agent
		self.contract = validate_contract(contract)
		self.storage = storage
		self.workflow_id = workflow_id
		self.run_id = run_id or f'run_{uuid4().hex}'
		self.history_path = (
			Path(history_path)
			if history_path is not None
			else (Path.cwd() / '.browser_use_recovery' / f'{hashlib.sha256(workflow_id.encode()).hexdigest()}.history.json')
		)
		self.last_effect_seq = last_effect_seq
		self._checkpoint_manager = CheckpointManager(storage)
		self._context_sink = _DeferredContextSink()
		self._prompt_memory = UnitPromptMemoryPolicy()
		if verifier is None:
			if any(unit.verification.source is VerificationSource.EXTERNAL_TOOL for unit in self.contract.units):
				raise ContractPolicyError('external_tool verification requires a read-only verifier')
			if any(
				unit.effect.has_side_effect
				and unit.verification.source is VerificationSource.BROWSER
				and unit.verification.observation_url is None
				for unit in self.contract.units
			):
				raise ContractPolicyError('side-effect browser verification requires an explicit observation_url')
			verifier = ObservationalVerifier(
				BrowserObservationVerifier(BrowserObservationSource(), LLMObservationInterpreter(agent.llm))
			)
		self.verifier = verifier
		self.adapter = BrowserUseRuntimeAdapter(
			contract=self.contract,
			states=states,
			verifier=verifier,
			claim_source=_DoneClaimSource(),
			context_sink=self._context_sink,
			protect_side_effect_completion=True,
		)
		self._coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=self._checkpoint_manager,
			workflow_id=workflow_id,
			run_id=self.run_id,
			contract=self.contract,
			verifier=verifier,
		)
		self._boundary_executor = EffectBoundaryExecutor(self._coordinator, self.adapter)
		self._original_get_next_action = None
		self._original_multi_act = None
		self._original_make_history_item = None
		self._history_count = len(agent.history.history)
		self._output_models: dict[type, type] = {}
		self._completed_unit_ids = {
			unit_id for unit_id, state in self.adapter.states.items() if state.status is UnitStatus.COMPLETED
		}
		if _start_run:
			storage.start_run(WorkflowRun(workflow_id=workflow_id, run_id=self.run_id))
			storage.save_contract(workflow_id, self.contract)
			self._save_checkpoint('initialized')

	@classmethod
	async def resume(
		cls,
		*,
		agent: Agent,
		storage: RuntimeStorage,
		workflow_id: str,
		run_id: str | None = None,
		history_path: str | Path | None = None,
		verifier: Verifier[object] | None = None,
	) -> RecoverableHarness:
		"""Restore durable state, reconcile uncertainty, then return a runnable harness."""
		recovered = RecoveryBootstrap(storage).restore(workflow_id, run_id or f'run_{uuid4().hex}')
		harness = cls(
			agent=agent,
			contract=recovered.contract,
			storage=storage,
			workflow_id=workflow_id,
			run_id=recovered.run_id,
			history_path=history_path,
			verifier=verifier,
			states=dict(recovered.states),
			last_effect_seq=recovered.last_effect_seq,
			_start_run=False,
		)
		if harness.history_path.exists():
			harness._extend_output_schema()
			agent.history = AgentHistoryList.load_from_file(harness.history_path, agent.AgentOutput)
			harness._history_count = len(agent.history.history)
			agent.state.n_steps = max(agent.state.n_steps, harness._history_count + 1)
		for unit_id, state in tuple(harness.adapter.states.items()):
			if state.status is UnitStatus.UNKNOWN:
				coordinator = ReconciliationCoordinator(
					storage=storage,
					checkpoint_manager=harness._checkpoint_manager,
					workflow_id=workflow_id,
					run_id=harness.run_id,
					contract=harness.contract,
					verifier=harness.verifier,
				)
				result = await coordinator.reconcile(
					unit_id=unit_id,
					states=harness.adapter.states,
					last_effect_seq=harness.last_effect_seq,
					context=agent,
				)
				harness.adapter.apply_runtime_states(result.states)
				harness.last_effect_seq = result.record.seq
		for unit_id in recovered.requires_reverification:
			states_now = harness.adapter.states
			if states_now[unit_id].status is UnitStatus.COMPLETION_CANDIDATE:
				manager = VerificationManager()
				updated, _ = await manager.verify_candidate(harness.contract, states_now, unit_id, harness.verifier, agent)
				harness.adapter.apply_runtime_states(updated)
				harness._save_checkpoint('resume_reverification')
		harness._prompt_memory.compact(agent, harness._prompt_memory.summaries(harness.contract, harness.adapter.states))
		return harness

	async def run(self, *, max_steps: int = 100) -> AgentHistoryList:
		"""Run the native Agent with recoverable step hooks and action seam."""
		if self._original_multi_act is not None:
			raise RuntimeError('harness is already running')
		self._original_multi_act = self.agent.multi_act
		self._original_get_next_action = self.agent._get_next_action
		self._original_make_history_item = self.agent._make_history_item
		self.agent.multi_act = MethodType(self._multi_act, self.agent)
		self.agent._get_next_action = MethodType(self._get_next_action, self.agent)
		self.agent._make_history_item = MethodType(self._make_history_item, self.agent)
		try:
			return await self.agent.run(max_steps=max_steps, on_step_start=self.on_step_start, on_step_end=self.on_step_end)
		finally:
			self.agent.multi_act = self._original_multi_act
			self.agent._get_next_action = self._original_get_next_action
			self.agent._make_history_item = self._original_make_history_item
			self._original_multi_act = None
			self._original_get_next_action = None
			self._original_make_history_item = None

	async def _make_history_item(
		self,
		agent: Agent,
		model_output: AgentOutput | None,
		browser_state_summary: BrowserStateSummary,
		result: list[ActionResult],
		metadata: StepMetadata | None = None,
		state_message: str | None = None,
	) -> None:
		"""Persist the native history item immediately after Browser Use appends it."""
		assert self._original_make_history_item is not None
		await self._original_make_history_item(model_output, browser_state_summary, result, metadata, state_message)
		self._persist_finalized_history()

	async def on_step_start(self, agent: Agent) -> None:
		"""Activate a ready unit and checkpoint that semantic transition."""
		before = self.adapter.states
		await self.adapter.on_step_start(agent)
		if before != self.adapter.states:
			self._save_checkpoint('unit_active')

	async def on_step_end(self, agent: Agent) -> None:
		"""Finish semantic bookkeeping and persist every finalized native step."""
		before = self.adapter.states
		try:
			await self.adapter.on_step_end(agent)
			if before != self.adapter.states:
				self._save_checkpoint('unit_transition')
			completed = {unit_id for unit_id, state in self.adapter.states.items() if state.status is UnitStatus.COMPLETED}
			if completed - self._completed_unit_ids:
				self._prompt_memory.compact(agent, self._prompt_memory.summaries(self.contract, self.adapter.states))
				# Native preparation would otherwise re-add the completed step's
				# model output and ActionResult to the next prompt.
				agent.state.last_model_output = None
				agent.state.last_result = None
			self._completed_unit_ids = completed
		finally:
			self._persist_finalized_history()

	async def _get_next_action(self, agent: Agent, browser_state_summary: BrowserStateSummary) -> None:
		"""Publish current semantics after native context preparation, then ask the model."""
		self._extend_output_schema()
		context = self._context_sink.context
		if context is not None:
			agent.message_manager._add_context_message(
				UserMessage(
					content=(
						context.render() + '\nIf an action crosses the current unit side-effect boundary, '
						'set effect_boundary_action_index to its zero-based index. '
						'Leave it null for preparation actions. The boundary action ends this step. '
						'For a completed non-effect unit, set completion_claim_unit_id to that unit ID. '
						'Use done only after all semantic units are completed.'
					)
				)
			)
		assert self._original_get_next_action is not None
		await self._original_get_next_action(browser_state_summary)

	async def _multi_act(self, agent: Agent, actions: list[ActionModel]) -> list[ActionResult]:
		"""Run normal preparation actions, then exactly one protected boundary action."""
		assert self._original_multi_act is not None
		if any(action.model_dump(exclude_none=True).get('done') is not None for action in actions):
			if any(state.status is not UnitStatus.COMPLETED for state in self.adapter.states.values()):
				raise CompletionClaimError('done requires all semantic units to be completed')
		active = self.adapter.last_context.active_unit if self.adapter.last_context else None
		if active is None:
			return await self._original_multi_act(actions)
		unit = self.contract.get_unit(active.unit_id)
		output = agent.state.last_model_output
		boundary_index = getattr(output, 'effect_boundary_action_index', None)
		if boundary_index is None:
			if unit.effect.has_side_effect and any(
				action.model_dump(exclude_none=True).get('done') is not None for action in actions
			):
				raise CompletionClaimError(f'{unit.unit_id}: done cannot bypass the effect boundary')
			return await self._original_multi_act(actions)
		plan = EffectBoundaryPlan.from_declaration(unit, len(actions), EffectBoundaryDeclaration(action_index=boundary_index))
		pre_url = await agent.browser_session.get_current_page_url() if plan.action_index else None
		pre_focus = getattr(agent.browser_session, 'agent_focus_target_id', None)
		prefix = await self._original_multi_act(actions[: plan.action_index]) if plan.action_index else []
		if len(prefix) != plan.action_index or any(item.error or item.is_done for item in prefix):
			return prefix
		if plan.action_index:
			post_url = await agent.browser_session.get_current_page_url()
			post_focus = getattr(agent.browser_session, 'agent_focus_target_id', None)
			if post_url != pre_url or post_focus != pre_focus:
				return prefix
			last_action = actions[plan.action_index - 1].model_dump(exclude_unset=True)
			last_name = next(iter(last_action), None)
			registry = getattr(getattr(getattr(agent, 'tools', None), 'registry', None), 'registry', None)
			registered = getattr(registry, 'actions', {}).get(last_name) if registry is not None else None
			if registered is not None and registered.terminates_sequence:
				return prefix
		original_multi_act = self._original_multi_act
		assert original_multi_act is not None
		results, self.last_effect_seq = await self._boundary_executor.execute(
			plan=plan,
			effect_key=unit.identity.intent_key,
			last_effect_seq=self.last_effect_seq,
			original_action=lambda: original_multi_act([actions[plan.action_index]]),
			verification_context=agent,
		)
		return prefix + results

	def _extend_output_schema(self) -> None:
		"""Add harness control metadata without changing native action schemas."""
		for attribute in ('AgentOutput', 'DoneAgentOutput'):
			base = getattr(self.agent, attribute, None)
			if base is None or 'effect_boundary_action_index' in base.model_fields:
				continue
			model = self._output_models.get(base)
			if model is None:
				model = create_model(
					'RecoverableAgentOutput',
					__base__=base,
					effect_boundary_action_index=(int | None, Field(default=None, ge=0)),
					completion_claim_unit_id=(str | None, Field(default=None)),
				)
				self._output_models[base] = model
			setattr(self.agent, attribute, model)

	def _save_checkpoint(self, reason: str) -> None:
		"""Save only at a meaningful semantic transition."""
		self._checkpoint_manager.save(
			workflow_id=self.workflow_id,
			run_id=self.run_id,
			contract=self.contract,
			states=self.adapter.states,
			last_effect_seq=self.last_effect_seq,
			metadata={'reason': reason},
		)

	def _persist_finalized_history(self) -> None:
		"""Atomically save native AgentHistory after each newly finalized step."""
		count = len(self.agent.history.history)
		if count <= self._history_count:
			return
		self.history_path.parent.mkdir(parents=True, exist_ok=True)
		data = self.agent.history.model_dump(sensitive_data=self.agent.sensitive_data)
		fd, temporary = tempfile.mkstemp(dir=self.history_path.parent, suffix='.tmp')
		try:
			with os.fdopen(fd, 'w', encoding='utf-8') as stream:
				json.dump(data, stream, ensure_ascii=False)
				stream.flush()
				os.fsync(stream.fileno())
			os.replace(temporary, self.history_path)
		except BaseException:
			Path(temporary).unlink(missing_ok=True)
			raise
		self._history_count = count
