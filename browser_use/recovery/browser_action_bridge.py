"""Action-level bridge routing dangerous Browser Use actions through the durable side-effect boundary.

Browser Use tools and actions normally execute web operations directly:

	LLM -> Tools -> Action -> direct web operation

The bridge registers explicit business side-effect actions (starting with
``submit_application``) whose real browser operation only runs inside
``SideEffectCoordinator.execute``, so the durable PREPARED ledger fact always
exists before the click and verification happens before the final state:

	LLM -> Tools -> submit_application -> SideEffectCoordinator.execute
	        -> PREPARED -> real click -> ATTEMPTED -> verification -> final record

This keeps the Harness in front of the side effect instead of observing it after
the fact, without touching Browser Use's generic ``click`` action.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser.session import BrowserSession
from browser_use.recovery.browser_use_adapter import BrowserUseRuntimeAdapter
from browser_use.recovery.side_effects import SideEffectCoordinator

if TYPE_CHECKING:
	from browser_use.actor.element import Element
	from browser_use.actor.page import Page
	from browser_use.llm.base import BaseChatModel
	from browser_use.tools.service import Tools


class BrowserActionBridgeError(RuntimeError):
	"""Raised when an action cannot be routed through the side-effect boundary."""


ElementFinder = Callable[['Page', 'BaseChatModel | None'], Awaitable['Element']]


class BrowserActionBridge:
	"""Register harness-managed custom actions on a Browser Use ``Tools`` registry.

	The registered action does not click the submit button directly. It wraps the
	real browser operation in an executor that ``SideEffectCoordinator`` runs
	between its durable PREPARED and ATTEMPTED ledger facts, then writes the
	authoritative Runtime snapshot back into the adapter and advances
	``last_effect_seq`` so SQLite, the adapter, and the bridge stay consistent.
	"""

	def __init__(
		self,
		*,
		tools: 'Tools',
		runtime_adapter: BrowserUseRuntimeAdapter,
		side_effect_coordinator: SideEffectCoordinator,
		last_effect_seq: int = 0,
		llm: 'BaseChatModel | None' = None,
		unit_id: str = 'u_submit',
		effect_key: str = 'submit_application',
		button_prompt: str = 'Submit application button',
		element_finder: ElementFinder | None = None,
		after_prepared_hook: Callable[[], None] | None = None,
		after_attempt_hook: Callable[[], None] | None = None,
		action_description: str = (
			'Submit the current application. Use only when the semantic runtime current unit requires submitting it.'
		),
	) -> None:
		self._tools = tools
		self._runtime_adapter = runtime_adapter
		self._coordinator = side_effect_coordinator
		self._last_effect_seq = last_effect_seq
		self._llm = llm
		self._unit_id = unit_id
		self._effect_key = effect_key
		self._button_prompt = button_prompt
		self._element_finder = element_finder or self._find_submit_button
		self._after_prepared_hook = after_prepared_hook
		self._after_attempt_hook = after_attempt_hook
		self._action_description = action_description
		self._registered = False

	async def _find_submit_button(self, page: 'Page', llm: 'BaseChatModel | None') -> 'Element':
		"""Locate the submit button by natural-language prompt (default strategy)."""
		return await page.must_get_element_by_prompt(self._button_prompt, llm)

	@property
	def last_effect_seq(self) -> int:
		"""Return the durable ledger sequence recorded after the latest attempt."""
		return self._last_effect_seq

	@property
	def unit_id(self) -> str:
		"""Return the Contract unit routed through the boundary."""
		return self._unit_id

	@property
	def effect_key(self) -> str:
		"""Return the durable effect key recorded in the ledger."""
		return self._effect_key

	def register(self) -> None:
		"""Register the harness-managed ``submit_application`` action on the Tools registry."""
		if self._registered:
			raise BrowserActionBridgeError('browser action bridge is already registered')
		bridge = self

		@self._tools.registry.action(self._action_description)
		async def submit_application(browser_session: BrowserSession) -> ActionResult:
			async def executor() -> dict[str, object]:
				page = await browser_session.must_get_current_page()
				button = await bridge._element_finder(page, bridge._llm)
				await button.click()
				return {'action': bridge._effect_key, 'submitted': True}

			result = await bridge._coordinator.execute(
				unit_id=bridge._unit_id,
				effect_key=bridge._effect_key,
				states=bridge._runtime_adapter.states,
				last_effect_seq=bridge._last_effect_seq,
				executor=executor,
				after_prepared_hook=bridge._after_prepared_hook,
				after_attempt_hook=bridge._after_attempt_hook,
			)
			bridge._runtime_adapter.apply_runtime_states(result.states)
			bridge._last_effect_seq = result.records[-1].seq

			phases = ', '.join(record.status.value for record in result.records)
			return ActionResult(
				extracted_content=(
					f'{bridge._effect_key} passed through the side-effect boundary: '
					f'{phases}; verification={result.verification_result.outcome.value}'
				)
			)

		self._registered = True
