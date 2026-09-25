"""Independent Browser Use workers with real process stops at six crash windows."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from browser_use import BrowserProfile
from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult, AgentOutput, StepMetadata
from browser_use.browser.session import BrowserSession
from browser_use.recovery.browser_use_adapter import RuntimeProgressBlockedError
from browser_use.recovery.contracts import EffectStatus, UnitStatus
from browser_use.recovery.harness import RecoverableHarness
from browser_use.recovery.observational_verifier import BrowserObservationSource, BrowserObservationVerifier
from browser_use.recovery.persistence.models import EffectRecordDraft, EffectRecordStatus
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from experiments.final_comparison.history import action_names, load_history, persist_history, replay_safe_history
from experiments.final_comparison.models import ActionTrace, Mode, Scenario
from experiments.final_comparison.workflow import PageInterpreter, WorkflowScript, build_contract

if TYPE_CHECKING:
	from browser_use.agent.views import ActionModel
	from browser_use.browser.views import BrowserStateSummary
	from browser_use.recovery.persistence.models import RuntimeCheckpoint

CRASH_EXIT_CODE = 91


def _record(path: Path, phase: str, action: str) -> None:
	"""Fsync action instrumentation before the worker may disappear."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open('a', encoding='utf-8') as stream:
		stream.write(ActionTrace(phase=phase, action=action).model_dump_json() + '\n')
		stream.flush()
		os.fsync(stream.fileno())


def _stop() -> None:
	"""Terminate without finally blocks or Browser Use cleanup running."""
	os._exit(CRASH_EXIT_CODE)


class FaultStorage(SQLiteRuntimeStorage):
	"""Stop after the durable ledger write at S1 or S3/S5."""

	def __init__(self, path: Path, scenario: Scenario) -> None:
		super().__init__(path)
		self.scenario = scenario

	def commit_effect_and_checkpoint(self, draft: EffectRecordDraft, checkpoint: RuntimeCheckpoint):
		"""Persist PREPARED before S1 stops the process."""
		result = super().commit_effect_and_checkpoint(draft, checkpoint)
		if draft.status is EffectRecordStatus.PREPARED and self.scenario is Scenario.BEFORE_EFFECT:
			_stop()
		return result

	def append_effect(self, draft: EffectRecordDraft):
		"""Persist ATTEMPTED before S3 or S5 stops the process."""
		record = super().append_effect(draft)
		if draft.status is EffectRecordStatus.ATTEMPTED and self.scenario in {
			Scenario.AFTER_ATTEMPTED_BEFORE_HISTORY,
			Scenario.VERIFIER_UNAVAILABLE,
		}:
			_stop()
		return record


class BenchmarkAgent(Agent):
	"""Persist native history and inject equal faults around native click."""

	def __init__(
		self,
		*,
		browser_session: BrowserSession,
		script: WorkflowScript,
		mode: Mode,
		scenario: Scenario,
		phase: str,
		trace_path: Path,
		history_path: Path | None = None,
	) -> None:
		self.benchmark_mode = mode
		self.benchmark_scenario = scenario
		self.benchmark_phase = phase
		self.benchmark_trace_path = trace_path
		self.benchmark_history_path = history_path
		super().__init__(
			task='Locate application #7, fill John Doe, and submit exactly once.',
			llm=script,
			browser_session=browser_session,
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

	async def multi_act(self, actions: list[ActionModel]) -> list[ActionResult]:
		"""Record native actions and stop at the shared click seams."""
		names = tuple(next(iter(action.model_dump(exclude_none=True)), '') for action in actions)
		click = 'click' in names
		if (
			self.benchmark_phase == 'initial'
			and self.benchmark_mode is Mode.NATIVE
			and self.benchmark_scenario is Scenario.BEFORE_EFFECT
			and click
		):
			_stop()
		result = await super().multi_act(actions)
		for name in names:
			if name:
				_record(self.benchmark_trace_path, self.benchmark_phase, name)
		if (
			self.benchmark_phase == 'initial'
			and click
			and (
				self.benchmark_scenario is Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED
				or (
					self.benchmark_mode is Mode.NATIVE
					and self.benchmark_scenario in {Scenario.AFTER_ATTEMPTED_BEFORE_HISTORY, Scenario.VERIFIER_UNAVAILABLE}
				)
			)
		):
			_stop()
		return result

	async def _make_history_item(
		self,
		model_output: AgentOutput | None,
		browser_state_summary: BrowserStateSummary,
		result: list[ActionResult],
		metadata: StepMetadata | None = None,
		state_message: str | None = None,
	) -> None:
		"""Fsync each finalized Native step, matching Harness history durability."""
		await super()._make_history_item(model_output, browser_state_summary, result, metadata, state_message)
		if self.benchmark_history_path is not None:
			persist_history(self, self.benchmark_history_path)

	async def _finalize(self, browser_state_summary: BrowserStateSummary | None) -> None:
		"""Stop S4 only after the click step's AgentHistory is durable."""
		await super()._finalize(browser_state_summary)
		if self.benchmark_phase == 'initial' and self.benchmark_scenario is Scenario.AFTER_HISTORY_COMMIT:
			last = self.history.history[-1]
			if last.model_output is not None and any(
				action.model_dump(exclude_none=True).get('click') is not None for action in last.model_output.action
			):
				_stop()


def _browser(*, keep_alive: bool = False) -> BrowserSession:
	"""Use one BrowserSession configuration for both comparison arms."""
	return BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=keep_alive))


def _agent(
	*,
	browser: BrowserSession,
	script: WorkflowScript,
	mode: Mode,
	scenario: Scenario,
	phase: str,
	trace_path: Path,
	history_path: Path | None,
) -> BenchmarkAgent:
	"""Construct both arms with equal native Agent settings."""
	return BenchmarkAgent(
		browser_session=browser,
		script=script,
		mode=mode,
		scenario=scenario,
		phase=phase,
		trace_path=trace_path,
		history_path=history_path,
	)


def _verifier(*, available: bool = True) -> BrowserObservationVerifier:
	return BrowserObservationVerifier(BrowserObservationSource(), PageInterpreter(available=available))


async def _initial(args: argparse.Namespace) -> None:
	browser = _browser()
	agent = _agent(
		browser=browser,
		script=WorkflowScript(args.server_url, harness=args.mode is Mode.HARNESS),
		mode=args.mode,
		scenario=args.scenario,
		phase='initial',
		trace_path=args.trace_file,
		history_path=args.history_file if args.mode is Mode.NATIVE else None,
	)
	if args.mode is Mode.NATIVE:
		await agent.run(max_steps=7)
	else:
		with FaultStorage(args.runtime_db, args.scenario) as storage:
			harness = RecoverableHarness(
				agent=agent,
				contract=build_contract(args.server_url),
				storage=storage,
				workflow_id=args.workflow_id,
				run_id='run-1',
				history_path=args.history_file,
				verifier=_verifier(),
			)
			await harness.run(max_steps=7)
	await browser.kill()


async def _replay(args: argparse.Namespace, browser: BrowserSession) -> tuple[str, ...]:
	reader = _agent(
		browser=browser,
		script=WorkflowScript(args.server_url, harness=False, start_step=3),
		mode=args.mode,
		scenario=args.scenario,
		phase='reader',
		trace_path=args.trace_file,
		history_path=None,
	)
	history = load_history(reader, args.history_file)
	replayed = await replay_safe_history(reader, history)
	for name in replayed:
		_record(args.trace_file, 'replay', name)
	return action_names(history)


async def _recover(args: argparse.Namespace) -> None:
	browser = _browser(keep_alive=True)
	try:
		persisted_actions = await _replay(args, browser)
		if args.mode is Mode.NATIVE:
			step = 3 if 'click' in persisted_actions else 2
			agent = _agent(
				browser=browser,
				script=WorkflowScript(args.server_url, harness=False, start_step=step),
				mode=args.mode,
				scenario=args.scenario,
				phase='recovery',
				trace_path=args.trace_file,
				history_path=args.history_file,
			)
			agent.history = load_history(agent, args.history_file)
			agent.state.n_steps = max(agent.state.n_steps, len(agent.history.history) + 1)
			await agent.run(max_steps=8)
		else:
			available = args.phase != 'observe_unavailable'
			script = WorkflowScript(args.server_url, harness=True, start_step=3)
			agent = _agent(
				browser=browser,
				script=script,
				mode=args.mode,
				scenario=args.scenario,
				phase='recovery',
				trace_path=args.trace_file,
				history_path=None,
			)
			with SQLiteRuntimeStorage(args.runtime_db) as storage:
				harness = await RecoverableHarness.resume(
					agent=agent,
					storage=storage,
					workflow_id=args.workflow_id,
					run_id='run-2'
					if args.phase == 'observe_unavailable'
					else ('run-3' if args.scenario is Scenario.VERIFIER_UNAVAILABLE else 'run-2'),
					history_path=args.history_file,
					verifier=_verifier(available=available),
				)
				state = harness.adapter.states['u_submit']
				if not available:
					if state.status is not UnitStatus.UNKNOWN:
						raise AssertionError(f'outage should remain UNKNOWN, got {state.status}')
					try:
						await harness.run(max_steps=8)
					except RuntimeProgressBlockedError:
						_record(args.trace_file, 'outage', 'blocked_by_runtime_gate')
					else:
						raise AssertionError('UNKNOWN effect was allowed to resume normal Agent actions')
					return
				if state.status is UnitStatus.ACTIVE and state.effect_status is EffectStatus.NOT_APPLIED:
					script.step = 1
					script.resume_prep = True
				await harness.run(max_steps=8)
	finally:
		await browser.kill()


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Final Native Browser Use vs RecoverableHarness worker')
	parser.add_argument('--phase', choices=('initial', 'recover', 'observe_unavailable'), required=True)
	parser.add_argument('--mode', type=Mode, choices=list(Mode), required=True)
	parser.add_argument('--scenario', type=Scenario, choices=list(Scenario), required=True)
	parser.add_argument('--server-url', required=True)
	parser.add_argument('--history-file', type=Path, required=True)
	parser.add_argument('--trace-file', type=Path, required=True)
	parser.add_argument('--runtime-db', type=Path, required=True)
	parser.add_argument('--workflow-id', required=True)
	return parser.parse_args()


def main() -> None:
	args = _parse_args()
	if args.phase == 'initial':
		asyncio.run(_initial(args))
	else:
		asyncio.run(_recover(args))


if __name__ == '__main__':
	main()
