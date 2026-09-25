"""Pinned compatibility checks for the three native Agent seams used by the harness."""

from __future__ import annotations

from inspect import iscoroutinefunction, signature
from typing import TYPE_CHECKING

from browser_use.recovery.browser_use_adapter import BrowserUseCompatibilityError

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


SUPPORTED_BROWSER_USE_VERSION = '0.13.10'

_SEAM_PARAMETERS = {
	'multi_act': ('actions',),
	'_get_next_action': ('browser_state_summary',),
	'_make_history_item': ('model_output', 'browser_state_summary', 'result', 'metadata', 'state_message'),
}


def assert_browser_use_compatibility(agent: Agent) -> None:
	"""Fail before changing Runtime state if the pinned native seam has drifted."""
	if agent.version != SUPPORTED_BROWSER_USE_VERSION:
		raise BrowserUseCompatibilityError(
			f'RecoverableHarness supports Browser Use {SUPPORTED_BROWSER_USE_VERSION}; found {agent.version}'
		)
	for name, expected_parameters in _SEAM_PARAMETERS.items():
		method = getattr(agent, name, None)
		if not iscoroutinefunction(method):
			raise BrowserUseCompatibilityError(f'Browser Use Agent.{name} is no longer an async method')
		actual_parameters = tuple(signature(method).parameters)
		if actual_parameters != expected_parameters:
			raise BrowserUseCompatibilityError(
				f'Browser Use Agent.{name} parameters changed: expected {expected_parameters}, found {actual_parameters}'
			)
