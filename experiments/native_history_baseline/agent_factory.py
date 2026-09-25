"""Shared Agent construction for Native History and Harness experiment arms."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser_use import BrowserProfile
from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult
from browser_use.browser.session import BrowserSession
from browser_use.recovery.browser_action_bridge import BrowserActionBridge
from browser_use.recovery.browser_use_adapter import BrowserUseRuntimeAdapter, CompletionClaim
from browser_use.recovery.side_effects import SideEffectCoordinator
from browser_use.tools.service import Tools
from experiments.native_history_baseline.models import CrashPoint
from experiments.native_history_baseline.scripted_llm import ScriptedLLM
from experiments.recovery_smoke.run_agent import find_submit_button_by_selector

if TYPE_CHECKING:
	from browser_use.browser.views import BrowserStateSummary
	from browser_use.recovery.contracts import SemanticUnit


class HistoryCommitCrash(BaseException):
	"""Fault-injection signal that bypasses Agent's ordinary Exception handling."""


@dataclass
class CrashController:
	"""Tell FaultInjectingAgent to skip the current step's AgentHistory commit."""

	crash_before_history_commit: bool = False

	def request_before_history_commit(self) -> None:
		self.crash_before_history_commit = True


class FaultInjectingAgent(Agent):
	"""Agent subclass used only to expose the external-effect -> history-commit seam."""

	def __init__(self, *args, crash_controller: CrashController | None = None, **kwargs) -> None:
		self._experiment_crash_controller = crash_controller or CrashController()
		super().__init__(*args, **kwargs)

	async def _finalize(self, browser_state_summary: 'BrowserStateSummary | None') -> None:
		if self._experiment_crash_controller.crash_before_history_commit:
			raise HistoryCommitCrash('injected crash before AgentHistory finalize')
		await super()._finalize(browser_state_summary)


class NoopClaimSource:
	"""The side-effect boundary owns completion; step hooks do not infer it from prose."""

	async def completion_claim(self, agent: Agent, unit: 'SemanticUnit') -> CompletionClaim | None:
		return None


def build_browser_session(*, headless: bool = True, keep_alive: bool = False) -> BrowserSession:
	return BrowserSession(
		browser_profile=BrowserProfile(
			headless=headless,
			user_data_dir=None,
			keep_alive=keep_alive,
		)
	)


def register_native_submit_action(
	tools: Tools,
	*,
	crash_point: CrashPoint | None,
	crash_controller: CrashController,
) -> None:
	"""Register direct Native submit_application without Harness semantics."""

	@tools.registry.action('Submit the current application.')
	async def submit_application(browser_session: BrowserSession) -> ActionResult:
		if crash_point is CrashPoint.BEFORE_EXTERNAL_EFFECT:
			crash_controller.request_before_history_commit()
			raise HistoryCommitCrash('injected crash before external submit')

		page = await browser_session.must_get_current_page()
		button = await find_submit_button_by_selector(page, None)
		await button.click()

		if crash_point is CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT:
			crash_controller.request_before_history_commit()
			raise HistoryCommitCrash('injected crash after external submit before AgentHistory finalize')

		return ActionResult(
			extracted_content='submit_application executed through Native Browser Use action',
			long_term_memory='Application submit action executed',
		)


def build_native_tools(
	*,
	crash_point: CrashPoint | None = None,
	crash_controller: CrashController | None = None,
) -> tuple[Tools, CrashController]:
	controller = crash_controller or CrashController()
	tools = Tools()
	register_native_submit_action(
		tools,
		crash_point=crash_point,
		crash_controller=controller,
	)
	return tools, controller


def register_harness_submit_action(
	*,
	tools: Tools,
	runtime_adapter: BrowserUseRuntimeAdapter,
	coordinator: SideEffectCoordinator,
	last_effect_seq: int,
	crash_point: CrashPoint | None,
	crash_controller: CrashController,
) -> BrowserActionBridge:
	"""Register Harness submit action with system-neutral crash points."""

	def before_external_effect() -> None:
		crash_controller.request_before_history_commit()
		raise HistoryCommitCrash('injected crash before external submit')

	def after_external_effect() -> None:
		crash_controller.request_before_history_commit()
		raise HistoryCommitCrash('injected crash after external submit before AgentHistory finalize')

	bridge = BrowserActionBridge(
		tools=tools,
		runtime_adapter=runtime_adapter,
		side_effect_coordinator=coordinator,
		last_effect_seq=last_effect_seq,
		unit_id='u_submit',
		element_finder=find_submit_button_by_selector,
		after_prepared_hook=before_external_effect if crash_point is CrashPoint.BEFORE_EXTERNAL_EFFECT else None,
		after_attempt_hook=(
			after_external_effect if crash_point is CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT else None
		),
	)
	bridge.register()
	return bridge


def build_agent(
	*,
	browser_session: BrowserSession,
	tools: Tools,
	actions: list[dict],
	crash_controller: CrashController | None = None,
) -> FaultInjectingAgent:
	"""Construct both arms with identical Browser Use Agent settings."""
	return FaultInjectingAgent(
		task='Open the controlled application page and submit application #7 exactly once.',
		llm=ScriptedLLM(actions),
		browser_session=browser_session,
		tools=tools,
		crash_controller=crash_controller,
		max_actions_per_step=1,
		use_vision=False,
		use_thinking=True,
		use_judge=False,
		directly_open_url=False,
		message_compaction=False,
		enable_planning=False,
		loop_detection_enabled=False,
		enable_signal_handler=False,
		final_response_after_failure=False,
	)
