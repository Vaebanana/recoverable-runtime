"""Equal per-step AgentHistory durability and safe replay for both arms."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import Field, create_model

from browser_use.agent.service import Agent
from browser_use.agent.views import AgentHistoryList

SAFE_ACTIONS = frozenset({'navigate', 'input'})


def persist_history(agent: Agent, path: Path) -> None:
	"""Atomically fsync each finalized native AgentHistory snapshot."""
	path.parent.mkdir(parents=True, exist_ok=True)
	data = agent.history.model_dump(sensitive_data=agent.sensitive_data)
	fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
	try:
		with os.fdopen(fd, 'w', encoding='utf-8') as stream:
			json.dump(data, stream, ensure_ascii=False)
			stream.flush()
			os.fsync(stream.fileno())
		os.replace(temporary, path)
	except BaseException:
		Path(temporary).unlink(missing_ok=True)
		raise


def action_names(history: AgentHistoryList) -> tuple[str, ...]:
	"""Read native action names from finalized AgentHistory items."""
	names: list[str] = []
	for item in history.history:
		if item.model_output is not None:
			for action in item.model_output.action:
				payload = action.model_dump(exclude_none=True, mode='json')
				if payload:
					names.append(next(iter(payload)))
	return tuple(names)


def load_history(agent: Agent, path: Path) -> AgentHistoryList:
	"""Load history using the action schema of the current Browser Use Agent."""
	reader_output = create_model(
		'BenchmarkHistoryOutput',
		__base__=agent.AgentOutput,
		completion_claim_unit_id=(str | None, Field(default=None)),
		effect_boundary_action_index=(int | None, Field(default=None, ge=0)),
	)
	return AgentHistoryList.load_from_file(path, reader_output)


async def replay_safe_history(agent: Agent, history: AgentHistoryList) -> tuple[str, ...]:
	"""Replay only the finalized navigate/input prefix; never replay click."""
	items = []
	for item in history.history:
		single = AgentHistoryList(history=[item])
		names = action_names(single)
		if not names or any(name not in SAFE_ACTIONS for name in names):
			break
		items.append(item)
	prefix = AgentHistoryList(history=items)
	if items:
		from experiments.native_history_baseline.scripted_llm import ScriptedLLM

		await agent.rerun_history(
			prefix,
			max_retries=1,
			skip_failures=False,
			delay_between_actions=0.0,
			max_step_interval=0.0,
			summary_llm=ScriptedLLM(),
			wait_for_elements=False,
		)
	return action_names(prefix)
