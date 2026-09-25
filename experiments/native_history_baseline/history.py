"""Browser Use AgentHistory loading and conservative safe-prefix replay."""

from __future__ import annotations

from pathlib import Path

from browser_use.agent.service import Agent
from browser_use.agent.views import AgentHistory, AgentHistoryList
from experiments.native_history_baseline.scripted_llm import ScriptedLLM

SAFE_REPLAY_ACTIONS = frozenset({'navigate'})


def _step_action_names(item: AgentHistory) -> tuple[str, ...]:
	if item.model_output is None:
		return ()
	names: list[str] = []
	for action in item.model_output.action:
		payload = action.model_dump(exclude_none=True, mode='json')
		if payload:
			names.append(next(iter(payload)))
	return tuple(names)


def history_action_names(history: AgentHistoryList) -> tuple[str, ...]:
	names: list[str] = []
	for item in history.history:
		names.extend(_step_action_names(item))
	return tuple(names)


def load_history(agent: Agent, history_path: Path) -> AgentHistoryList:
	return AgentHistoryList.load_from_file(history_path, agent.AgentOutput)


def safe_prefix(history: AgentHistoryList) -> AgentHistoryList:
	items: list[AgentHistory] = []
	for item in history.history:
		names = _step_action_names(item)
		if not names or any(name not in SAFE_REPLAY_ACTIONS for name in names):
			break
		items.append(item)
	return AgentHistoryList(history=items)


async def replay_safe_prefix(agent: Agent, history: AgentHistoryList) -> tuple[str, ...]:
	prefix = safe_prefix(history)
	names = history_action_names(prefix)
	if not prefix.history:
		return names

	await agent.rerun_history(
		prefix,
		max_retries=1,
		skip_failures=False,
		delay_between_actions=0.0,
		max_step_interval=0.0,
		summary_llm=ScriptedLLM(),
		wait_for_elements=False,
	)
	return names
