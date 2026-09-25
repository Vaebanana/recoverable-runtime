"""Deterministic LLM used to remove model randomness from the recovery experiment."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from pydantic import BaseModel

from browser_use.agent.views import RerunSummaryAction
from browser_use.llm.views import ChatInvokeCompletion


def _agent_output(*, next_goal: str, action: dict[str, dict[str, Any]], memory: str = '') -> dict[str, Any]:
	return {
		'thinking': 'deterministic experiment action',
		'evaluation_previous_goal': 'scripted experiment state',
		'memory': memory,
		'next_goal': next_goal,
		'action': [action],
	}


def navigate_output(server_url: str) -> dict[str, Any]:
	return _agent_output(
		next_goal='Open the controlled application page',
		action={'navigate': {'url': server_url}},
	)


def submit_output() -> dict[str, Any]:
	return _agent_output(
		next_goal='Submit application #7 exactly once',
		action={'submit_application': {}},
	)


def done_output() -> dict[str, Any]:
	return _agent_output(
		next_goal='Task completed',
		memory='Application workflow finished',
		action={'done': {'text': 'Application workflow finished', 'success': True}},
	)


class ScriptedLLM:
	"""Minimal Browser Use-compatible deterministic model."""

	model = 'native-history-scripted'
	provider = 'mock'
	name = 'native-history-scripted'
	model_name = 'native-history-scripted'
	_verified_api_keys = True

	def __init__(self, actions: Sequence[dict[str, Any]] = ()) -> None:
		self._actions = list(actions)
		self._index = 0

	@classmethod
	def __get_pydantic_core_schema__(
		cls,
		source_type: type,
		handler: Any,
	) -> Any:
		"""Satisfy Browser Use's BaseChatModel protocol for Pydantic fields."""
		from pydantic_core import core_schema

		return core_schema.any_schema()

	def _next_action(self) -> dict[str, Any]:
		if self._index < len(self._actions):
			action = self._actions[self._index]
			self._index += 1
			return action
		return done_output()

	async def ainvoke(self, *args: object, **kwargs: object) -> ChatInvokeCompletion:
		output_format = kwargs.get('output_format')
		if output_format is None and len(args) >= 2:
			output_format = args[1]

		if output_format is RerunSummaryAction:
			summary = RerunSummaryAction(
				summary='Safe native history prefix replayed successfully',
				success=True,
				completion_status='complete',
			)
			return ChatInvokeCompletion(completion=summary, usage=None)

		action = self._next_action()
		if output_format is None:
			return ChatInvokeCompletion(completion=json.dumps(action), usage=None)

		model_validate = getattr(output_format, 'model_validate', None)
		if not callable(model_validate):
			return ChatInvokeCompletion(completion=json.dumps(action), usage=None)

		completion = cast(BaseModel, model_validate(action))
		return ChatInvokeCompletion(completion=completion, usage=None)
