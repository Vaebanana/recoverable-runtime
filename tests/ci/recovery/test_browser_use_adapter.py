from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from browser_use.agent.views import ActionResult
from browser_use.recovery.browser_use_adapter import (
	BrowserUseCompatibilityError,
	BrowserUseMessageContextSink,
	BrowserUseRuntimeAdapter,
	CompletionClaim,
	CompletionClaimError,
	RuntimeProgressBlockedError,
	UnitSelectionRequiredError,
)
from browser_use.recovery.contracts import (
	ConditionSpec,
	EffectSpec,
	EffectStatus,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	UnitStatus,
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.runtime_state import RuntimeStateManager
from browser_use.recovery.semantic_context import SemanticRuntimeContext
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


def make_unit(unit_id: str, target_key: str = 'company_a/job_123') -> SemanticUnit:
	"""Build a side-effecting semantic unit for adapter tests."""
	return SemanticUnit(
		unit_id=unit_id,
		identity=UnitIdentity(
			intent_key='submit_application',
			target_key=target_key,
			outcome_key='application_submitted',
		),
		intent='submit application',
		target=TargetSpec(type='job', key=target_key, attributes={'job_id': target_key.rsplit('/', 1)[-1]}),
		postconditions=(ConditionSpec(description='application exists with submitted status'),),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
		),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='inspect application history',
		),
	)


def make_contract(*units: SemanticUnit) -> SemanticContract:
	"""Build an adapter Contract snapshot."""
	return SemanticContract(contract_id='c1', task_id='task-1', version=1, units=units)


@dataclass
class FakeAgentState:
	"""Minimal shape consumed by the post-step trace adapter."""

	n_steps: int = 1
	last_result: list[ActionResult] | None = None
	last_model_output: object | None = None


@dataclass
class FakeAgent:
	"""Minimal hook-compatible agent object."""

	state: FakeAgentState = field(default_factory=FakeAgentState)
	message_manager: object | None = None


class CollectingSink:
	"""Collect semantic contexts without coupling tests to MessageManager."""

	def __init__(self) -> None:
		self.contexts: list[SemanticRuntimeContext] = []

	async def publish(self, agent: Agent, context: SemanticRuntimeContext) -> None:
		self.contexts.append(context)


class StaticClaimSource:
	"""Return a configured structured completion claim."""

	def __init__(self, claim: CompletionClaim | None) -> None:
		self.claim = claim

	async def completion_claim(self, agent: Agent, unit: SemanticUnit) -> CompletionClaim | None:
		return self.claim


class StaticVerifier:
	"""Return deterministic browser evidence for the active unit."""

	observational = True

	def __init__(self, outcome: VerificationOutcome) -> None:
		self.outcome = outcome

	async def verify(self, unit: SemanticUnit, context: Agent) -> VerificationResult:
		return VerificationResult(
			outcome=self.outcome,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed={'job_id': unit.target.attributes['job_id'], 'status': 'submitted'},
				source=VerificationSource.BROWSER,
				summary='target application was inspected',
			),
		)


def as_agent(fake: FakeAgent) -> Agent:
	"""Express deliberate structural test compatibility to the type checker."""
	return cast('Agent', fake)


def make_adapter(
	contract: SemanticContract,
	claim: CompletionClaim | None = None,
	outcome: VerificationOutcome = VerificationOutcome.VERIFIED,
) -> tuple[BrowserUseRuntimeAdapter, CollectingSink]:
	"""Build an adapter with deterministic collaborators."""
	sink = CollectingSink()
	adapter = BrowserUseRuntimeAdapter(
		contract=contract,
		verifier=StaticVerifier(outcome),
		claim_source=StaticClaimSource(claim),
		context_sink=sink,
	)
	return adapter, sink


@pytest.mark.asyncio
async def test_start_hook_auto_activates_the_only_ready_unit_and_publishes_context() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, sink = make_adapter(contract)
	agent = as_agent(FakeAgent())

	assert await adapter.on_step_start(agent) is None

	assert adapter.states['u1'].status is UnitStatus.ACTIVE
	assert sink.contexts[-1].active_unit is not None
	assert sink.contexts[-1].active_unit.unit_id == 'u1'
	assert adapter.last_context is sink.contexts[-1]


@pytest.mark.asyncio
async def test_start_hook_requires_explicit_selection_when_multiple_units_are_ready() -> None:
	contract = make_contract(make_unit('u1', 'target/job_1'), make_unit('u2', 'target/job_2'))
	adapter, sink = make_adapter(contract)

	with pytest.raises(UnitSelectionRequiredError, match='u1.*u2'):
		await adapter.on_step_start(as_agent(FakeAgent()))

	assert all(state.status is UnitStatus.PENDING for state in adapter.states.values())
	assert sink.contexts[-1].ready_unit_ids == ('u1', 'u2')

	adapter.activate('u2')
	await adapter.on_step_start(as_agent(FakeAgent()))
	assert adapter.states['u2'].status is UnitStatus.ACTIVE


@pytest.mark.asyncio
async def test_unknown_state_publishes_blocked_context_then_stops_normal_progress() -> None:
	contract = make_contract(make_unit('u1'))
	states = RuntimeStateManager().initialize(contract)
	states['u1'] = states['u1'].model_copy(update={'status': UnitStatus.UNKNOWN, 'effect_status': EffectStatus.UNKNOWN})
	sink = CollectingSink()
	adapter = BrowserUseRuntimeAdapter(
		contract=contract,
		states=states,
		verifier=StaticVerifier(VerificationOutcome.VERIFIED),
		claim_source=StaticClaimSource(None),
		context_sink=sink,
	)

	with pytest.raises(RuntimeProgressBlockedError, match='u1'):
		await adapter.on_step_start(as_agent(FakeAgent()))

	assert sink.contexts[-1].progress_blocked is True
	assert adapter.states['u1'].status is UnitStatus.UNKNOWN


@pytest.mark.asyncio
async def test_end_hook_records_a_compact_trace_without_claiming_completion() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract)
	agent = FakeAgent()
	await adapter.on_step_start(as_agent(agent))
	agent.state.n_steps = 7
	agent.state.last_model_output = SimpleNamespace(action=[object(), object()])
	agent.state.last_result = [
		ActionResult(extracted_content='page inspected'),
		ActionResult(error='button was not found'),
	]

	assert await adapter.on_step_end(as_agent(agent)) is None

	record = adapter.trace[-1]
	assert record.step_number == 7
	assert record.active_unit_id == 'u1'
	assert record.model_output_present is True
	assert record.action_count == 2
	assert record.result_count == 2
	assert record.result_errors == ('button was not found',)
	assert record.completion_claim is None
	assert record.verification_result is None
	assert adapter.states['u1'].status is UnitStatus.ACTIVE


@pytest.mark.asyncio
async def test_false_completion_claim_does_not_start_verification() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract, CompletionClaim(candidate_unit_id='u1', claimed_complete=False))
	agent = FakeAgent()
	await adapter.on_step_start(as_agent(agent))

	await adapter.on_step_end(as_agent(agent))

	assert adapter.states['u1'].status is UnitStatus.ACTIVE
	assert adapter.trace[-1].completion_claim is not None
	assert adapter.trace[-1].verification_result is None


@pytest.mark.asyncio
async def test_structured_claim_runs_verification_and_updates_runtime_state() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract, CompletionClaim(candidate_unit_id='u1', claimed_complete=True))
	agent = FakeAgent()
	await adapter.on_step_start(as_agent(agent))

	await adapter.on_step_end(as_agent(agent))

	assert adapter.states['u1'].status is UnitStatus.COMPLETED
	assert adapter.states['u1'].effect_status is EffectStatus.COMMITTED
	assert adapter.trace[-1].verification_result is not None
	assert adapter.trace[-1].verification_result.outcome is VerificationOutcome.VERIFIED
	assert contract.version == 1


@pytest.mark.asyncio
async def test_wrong_unit_completion_claim_is_rejected_without_state_change() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract, CompletionClaim(candidate_unit_id='u2', claimed_complete=True))
	agent = FakeAgent()
	await adapter.on_step_start(as_agent(agent))

	with pytest.raises(CompletionClaimError, match='active unit u1'):
		await adapter.on_step_end(as_agent(agent))

	assert adapter.states['u1'].status is UnitStatus.ACTIVE


class FakeMessageManager:
	"""Record messages sent through Browser Use's compatibility seam."""

	def __init__(self) -> None:
		self.messages: list[object] = []

	def _add_context_message(self, message: object) -> None:
		self.messages.append(message)


@pytest.mark.asyncio
async def test_default_message_sink_isolates_browser_use_private_api() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract)
	context = adapter.context_builder.build(contract, adapter.states)
	manager = FakeMessageManager()
	agent = as_agent(FakeAgent(message_manager=manager))

	await BrowserUseMessageContextSink().publish(agent, context)

	message = manager.messages[0]
	assert getattr(message, 'text') == context.render()


@pytest.mark.asyncio
async def test_default_message_sink_reports_browser_use_api_incompatibility() -> None:
	contract = make_contract(make_unit('u1'))
	adapter, _ = make_adapter(contract)
	context = adapter.context_builder.build(contract, adapter.states)
	agent = as_agent(FakeAgent(message_manager=object()))

	with pytest.raises(BrowserUseCompatibilityError, match='_add_context_message'):
		await BrowserUseMessageContextSink().publish(agent, context)
