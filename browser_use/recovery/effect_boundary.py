"""Typed declaration and validation of a model-selected browser action boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import Field

from browser_use.agent.views import ActionResult
from browser_use.recovery.browser_use_adapter import BrowserUseRuntimeAdapter
from browser_use.recovery.contracts import FrozenModel, SemanticUnit
from browser_use.recovery.side_effects import SideEffectCoordinator


class EffectBoundaryDeclaration(FrozenModel):
	"""The single action index that crosses the active unit's effect boundary."""

	action_index: int = Field(ge=0)


class EffectBoundaryPlan(FrozenModel):
	"""Validated action index for one agent step."""

	unit_id: str
	action_index: int = Field(ge=0)

	@classmethod
	def from_declaration(
		cls, unit: SemanticUnit, action_count: int, declaration: EffectBoundaryDeclaration
	) -> EffectBoundaryPlan:
		"""Reject a boundary on a plain unit or outside the emitted action list."""
		if not unit.effect.has_side_effect:
			raise ValueError(f'{unit.unit_id}: a non-effect unit cannot declare a boundary')
		if declaration.action_index >= action_count:
			raise ValueError(f'{unit.unit_id}: effect boundary action index is out of range')
		return cls(unit_id=unit.unit_id, action_index=declaration.action_index)


class EffectBoundaryExecutor:
	"""Execute one original Browser Use action inside the durable boundary."""

	def __init__(self, coordinator: SideEffectCoordinator, adapter: BrowserUseRuntimeAdapter) -> None:
		self._coordinator = coordinator
		self._adapter = adapter

	async def execute(
		self,
		*,
		plan: EffectBoundaryPlan,
		effect_key: str,
		last_effect_seq: int,
		original_action: Callable[[], Awaitable[list[ActionResult]]],
		verification_context: object,
	) -> tuple[list[ActionResult], int]:
		"""Persist PREPARED, run the native action once, then observe and close."""
		action_results: list[ActionResult] = []

		async def execute_original_action() -> list[ActionResult]:
			result = await original_action()
			if not result or any(item.error for item in result):
				raise RuntimeError(result[0].error if result else 'native action returned no result')
			action_results.extend(result)
			return result

		outcome = await self._coordinator.execute(
			unit_id=plan.unit_id,
			effect_key=effect_key,
			states=self._adapter.states,
			last_effect_seq=last_effect_seq,
			executor=execute_original_action,
			verifier_context=verification_context,
		)
		self._adapter.apply_runtime_states(outcome.states)
		if not outcome.action_succeeded:
			action_results.append(ActionResult(error=outcome.action_error or 'effect action failed'))
		return action_results, outcome.records[-1].seq
