"""Contract validation and action-boundary safety for the generic harness."""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest

from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult
from browser_use.recovery.browser_use_adapter import (
	BrowserUseCompatibilityError,
	BrowserUseRuntimeAdapter,
	CompletionClaim,
	CompletionClaimError,
)
from browser_use.recovery.browser_use_compatibility import SUPPORTED_BROWSER_USE_VERSION, assert_browser_use_compatibility
from browser_use.recovery.contract_validation import ContractPolicyError, validate_contract
from browser_use.recovery.contracts import Reversibility, VerificationSource
from browser_use.recovery.effect_boundary import EffectBoundaryDeclaration, EffectBoundaryPlan
from browser_use.recovery.harness import RecoverableHarness
from browser_use.recovery.verification import VerificationOutcome
from experiments.native_history_baseline.scripted_llm import ScriptedLLM, done_output
from tests.ci.recovery.test_browser_use_adapter import (
	CollectingSink,
	FakeAgent,
	StaticClaimSource,
	StaticVerifier,
	as_agent,
	make_contract,
	make_unit,
)


def test_side_effect_requires_observable_postcondition_and_verification() -> None:
	unit = make_unit('u1').model_copy(
		update={'postconditions': (), 'verification': make_unit('u1').verification.model_copy(update={'required': False})}
	)
	with pytest.raises(ContractPolicyError):
		validate_contract(make_contract(unit))


def test_postcondition_requires_expected_observation() -> None:
	unit = make_unit('u1')
	unit = unit.model_copy(update={'postconditions': (unit.postconditions[0].model_copy(update={'expected_observation': None}),)})
	with pytest.raises(ContractPolicyError, match='expected_observation'):
		validate_contract(make_contract(unit))


def test_observation_url_requires_read_only_assertion() -> None:
	unit = make_unit('u1')
	unit = unit.model_copy(
		update={'verification': unit.verification.model_copy(update={'observation_url': 'https://example.test/status'})}
	)
	with pytest.raises(ContractPolicyError, match='read-only assertion'):
		validate_contract(make_contract(unit))


def test_read_only_procedure_can_name_a_mutating_button() -> None:
	unit = make_unit('u1')
	unit = unit.model_copy(
		update={'verification': unit.verification.model_copy(update={'procedure': 'inspect delete button state'})}
	)
	assert validate_contract(make_contract(unit)).get_unit('u1').verification.procedure == 'inspect delete button state'


def test_external_tool_source_is_outside_harness_v1() -> None:
	unit = make_unit('u1')
	unit = unit.model_copy(
		update={'verification': unit.verification.model_copy(update={'source': VerificationSource.EXTERNAL_TOOL})}
	)
	with pytest.raises(ContractPolicyError, match='EXTERNAL_TOOL'):
		validate_contract(make_contract(unit))


def test_harness_native_seams_match_pinned_browser_use_version() -> None:
	agent = Agent(task='Check native seams', llm=ScriptedLLM([done_output()]), enable_signal_handler=False)
	assert agent.version == SUPPORTED_BROWSER_USE_VERSION
	assert_browser_use_compatibility(agent)


def test_harness_rejects_changed_browser_use_version() -> None:
	agent = Agent(task='Check version pin', llm=ScriptedLLM([done_output()]), enable_signal_handler=False)
	agent.version = '0.13.11'
	with pytest.raises(BrowserUseCompatibilityError, match=SUPPORTED_BROWSER_USE_VERSION):
		assert_browser_use_compatibility(agent)


@pytest.mark.parametrize('seam', ['multi_act', '_get_next_action', '_make_history_item'])
def test_harness_rejects_changed_native_seam(seam: str, monkeypatch: pytest.MonkeyPatch) -> None:
	agent = Agent(task='Check changed seam', llm=ScriptedLLM([done_output()]), enable_signal_handler=False)

	async def incompatible(self) -> None:
		return None

	monkeypatch.setattr(agent, seam, MethodType(incompatible, agent))
	with pytest.raises(BrowserUseCompatibilityError, match=seam):
		assert_browser_use_compatibility(agent)


def test_obvious_effect_semantics_are_conservatively_upgraded() -> None:
	unit = make_unit('u1').model_copy(
		update={
			'effect': make_unit('u1').effect.model_copy(
				update={
					'has_side_effect': False,
					'reversibility': Reversibility.NOT_APPLICABLE,
				}
			),
		}
	)
	validated = validate_contract(make_contract(unit)).get_unit('u1')
	assert validated.effect.has_side_effect
	assert validated.effect.idempotency.value == 'unknown'
	assert validated.effect.reversibility.value == 'unknown'


def test_chinese_submit_intent_is_conservatively_upgraded() -> None:
	unit = make_unit('u1').model_copy(
		update={
			'intent': '提交申请',
			'effect': make_unit('u1').effect.model_copy(update={'has_side_effect': False}),
		}
	)
	assert validate_contract(make_contract(unit)).get_unit('u1').effect.has_side_effect


def test_boundary_index_must_be_valid_and_only_on_side_effect_unit() -> None:
	unit = make_unit('u1')
	assert EffectBoundaryPlan.from_declaration(unit, 3, EffectBoundaryDeclaration(action_index=1)).action_index == 1
	with pytest.raises(ValueError):
		EffectBoundaryPlan.from_declaration(unit, 1, EffectBoundaryDeclaration(action_index=1))
	plain = unit.model_copy(update={'effect': unit.effect.model_copy(update={'has_side_effect': False})})
	with pytest.raises(ValueError):
		EffectBoundaryPlan.from_declaration(plain, 1, EffectBoundaryDeclaration(action_index=0))


@pytest.mark.asyncio
async def test_plain_completion_claim_cannot_complete_effect_unit() -> None:
	adapter = BrowserUseRuntimeAdapter(
		contract=make_contract(make_unit('u1')),
		verifier=StaticVerifier(VerificationOutcome.VERIFIED),
		claim_source=StaticClaimSource(CompletionClaim(candidate_unit_id='u1', claimed_complete=True)),
		context_sink=CollectingSink(),
		protect_side_effect_completion=True,
	)
	agent = as_agent(FakeAgent())
	await adapter.on_step_start(agent)
	with pytest.raises(CompletionClaimError, match='effect boundary'):
		await adapter.on_step_end(agent)


@pytest.mark.asyncio
async def test_navigation_before_boundary_stops_stale_action_sequence() -> None:
	harness = object.__new__(RecoverableHarness)
	harness.contract = make_contract(make_unit('u1'))
	object.__setattr__(
		harness, 'adapter', SimpleNamespace(last_context=SimpleNamespace(active_unit=SimpleNamespace(unit_id='u1')))
	)
	harness.last_effect_seq = 0
	url = {'value': 'https://example.test/form'}

	class Session:
		async def get_current_page_url(self) -> str:
			return url['value']

	class Boundary:
		async def execute(self, **kwargs):
			raise AssertionError('stale boundary action was executed')

	async def native(actions):
		url['value'] = 'https://example.test/other'
		return [ActionResult(extracted_content='navigated')]

	harness._original_multi_act = native
	object.__setattr__(harness, '_boundary_executor', Boundary())
	actions = [
		SimpleNamespace(model_dump=lambda **kwargs: {'navigate': {'url': url['value']}}),
		SimpleNamespace(model_dump=lambda **kwargs: {'click': {'index': 1}}),
	]
	agent = SimpleNamespace(
		browser_session=Session(),
		state=SimpleNamespace(last_model_output=SimpleNamespace(effect_boundary_action_index=1)),
	)
	result = await harness._multi_act(cast(Any, agent), cast(Any, actions))
	assert len(result) == 1
