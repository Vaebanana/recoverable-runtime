"""Completed units leave only deterministic summaries in prompt memory."""

from __future__ import annotations

from browser_use.recovery.contracts import UnitStatus
from browser_use.recovery.prompt_memory import UnitPromptMemoryPolicy
from browser_use.recovery.runtime_state import RuntimeStateManager
from tests.ci.recovery.test_browser_use_adapter import make_contract, make_unit


def test_completed_unit_summary_uses_contract_and_state_only() -> None:
	contract = make_contract(make_unit('u1'))
	states = RuntimeStateManager().initialize(contract)
	policy = UnitPromptMemoryPolicy()
	summary = policy.summaries(contract, states)
	assert summary == ()
	states['u1'] = states['u1'].model_copy(update={'status': UnitStatus.COMPLETED})
	assert policy.summaries(contract, states)[0].unit_id == 'u1'
