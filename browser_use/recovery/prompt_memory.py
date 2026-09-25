"""Deterministic prompt summaries for completed semantic units."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from browser_use.recovery.contracts import FrozenModel, SemanticContract, UnitRuntimeState, UnitStatus

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


class CompletedUnitSummary(FrozenModel):
	"""Small model-visible record of a verified, completed unit."""

	unit_id: str
	intent: str
	target: str
	status: UnitStatus
	verified_facts: tuple[str, ...]

	def render(self) -> str:
		"""Render a compact deterministic history line."""
		return f'{self.unit_id} {self.intent} ({self.target}) -> {self.status.value}; ' + '; '.join(self.verified_facts)


class UnitPromptMemoryPolicy:
	"""Compact prompt memory at the semantic unit completion boundary."""

	def summaries(self, contract: SemanticContract, states: Mapping[str, UnitRuntimeState]) -> tuple[CompletedUnitSummary, ...]:
		"""Rebuild summaries from durable contract and runtime state."""
		return tuple(
			CompletedUnitSummary(
				unit_id=unit.unit_id,
				intent=unit.intent,
				target=unit.target.key,
				status=UnitStatus.COMPLETED,
				verified_facts=tuple(condition.description for condition in unit.postconditions),
			)
			for unit in contract.units
			if states[unit.unit_id].status is UnitStatus.COMPLETED
		)

	def compact(self, agent: Agent, summaries: tuple[CompletedUnitSummary, ...]) -> None:
		"""Drop old Browser steps from live prompt state while keeping native AgentHistory."""
		from browser_use.agent.message_manager.views import HistoryItem

		manager = agent.message_manager
		manager.state.agent_history_items = [
			HistoryItem(step_number=0, system_message='Agent initialized'),
			HistoryItem(system_message='Completed semantic units:\n' + '\n'.join(item.render() for item in summaries)),
		]
