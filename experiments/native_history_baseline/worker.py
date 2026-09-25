"""Independent workers for Native AgentHistory versus Harness recovery."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from browser_use.agent.service import Agent
from browser_use.recovery import EffectStatus, ReconciliationCoordinator, RecoveryBootstrap, UnitStatus
from browser_use.recovery.browser_use_adapter import BrowserUseRuntimeAdapter
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.recovery.side_effects import SideEffectCoordinator
from browser_use.tools.service import Tools
from experiments.native_history_baseline.agent_factory import (
	CrashController,
	HistoryCommitCrash,
	NoopClaimSource,
	build_agent,
	build_browser_session,
	build_native_tools,
	register_harness_submit_action,
)
from experiments.native_history_baseline.history import history_action_names, load_history, replay_safe_prefix
from experiments.native_history_baseline.models import CrashPoint, RecoveryMode
from experiments.native_history_baseline.scripted_llm import done_output, navigate_output, submit_output
from experiments.process_recovery.scenario import UNIT_ID, HttpApplicationStatusVerifier, build_contract

CRASH_EXIT_CODE = 91


def _last_finalized_action(agent: Agent) -> str | None:
	if not agent.history.history:
		return None
	last = agent.history.history[-1]
	if last.model_output is None or not last.model_output.action:
		return None
	payload = last.model_output.action[-1].model_dump(exclude_none=True, mode='json')
	return next(iter(payload)) if payload else None


async def _persist_history_and_maybe_crash(
	agent: Agent,
	*,
	history_path: Path,
	crash_point: CrashPoint,
	adapter: BrowserUseRuntimeAdapter | None = None,
) -> None:
	if adapter is not None:
		await adapter.on_step_end(agent)
	agent.save_history(history_path)

	if crash_point is CrashPoint.AFTER_HISTORY_COMMIT and _last_finalized_action(agent) == 'submit_application':
		raise HistoryCommitCrash('injected crash after durable AgentHistory commit')


async def _run_native_crash(
	*,
	server_url: str,
	history_path: Path,
	crash_point: CrashPoint,
	headless: bool,
) -> None:
	browser = build_browser_session(headless=headless)
	tools, controller = build_native_tools(
		crash_point=crash_point,
		crash_controller=CrashController(),
	)
	agent = build_agent(
		browser_session=browser,
		tools=tools,
		actions=[navigate_output(server_url), submit_output(), done_output()],
		crash_controller=controller,
	)

	async def on_step_end(current: Agent) -> None:
		await _persist_history_and_maybe_crash(
			current,
			history_path=history_path,
			crash_point=crash_point,
		)

	try:
		await agent.run(max_steps=4, on_step_end=on_step_end)
	finally:
		await browser.kill()

	raise AssertionError('Native crash phase completed without hitting the configured crash point')


async def _run_harness_crash(
	*,
	server_url: str,
	history_path: Path,
	runtime_db: Path,
	workflow_id: str,
	run_id: str,
	crash_point: CrashPoint,
	headless: bool,
) -> None:
	browser = build_browser_session(headless=headless)
	contract = build_contract()
	controller = CrashController()

	with SQLiteRuntimeStorage(runtime_db) as storage:
		storage.start_run(WorkflowRun(workflow_id=workflow_id, run_id=run_id))
		storage.save_contract(workflow_id, contract)

		verifier = HttpApplicationStatusVerifier(server_url)
		adapter = BrowserUseRuntimeAdapter(
			contract=contract,
			verifier=verifier,
			claim_source=NoopClaimSource(),
		)
		adapter.activate(UNIT_ID)

		coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=workflow_id,
			run_id=run_id,
			contract=contract,
			verifier=verifier,
		)
		tools = Tools()
		register_harness_submit_action(
			tools=tools,
			runtime_adapter=adapter,
			coordinator=coordinator,
			last_effect_seq=0,
			crash_point=crash_point,
			crash_controller=controller,
		)
		agent = build_agent(
			browser_session=browser,
			tools=tools,
			actions=[navigate_output(server_url), submit_output(), done_output()],
			crash_controller=controller,
		)

		async def on_step_end(current: Agent) -> None:
			await _persist_history_and_maybe_crash(
				current,
				history_path=history_path,
				crash_point=crash_point,
				adapter=adapter,
			)

		try:
			await agent.run(
				max_steps=4,
				on_step_start=adapter.on_step_start,
				on_step_end=on_step_end,
			)
		finally:
			await browser.kill()

	raise AssertionError('Harness crash phase completed without hitting the configured crash point')


async def _load_and_replay_history(*, browser, history_path: Path) -> tuple[str, ...]:
	reader_tools, reader_controller = build_native_tools()
	reader = build_agent(
		browser_session=browser,
		tools=reader_tools,
		actions=[done_output()],
		crash_controller=reader_controller,
	)
	history = load_history(reader, history_path)
	await replay_safe_prefix(reader, history)
	return history_action_names(history)


async def _run_native_recovery(
	*,
	server_url: str,
	history_path: Path,
	headless: bool,
) -> None:
	"""Resume using only Browser Use AgentHistory as durable completion evidence."""
	# rerun_history() closes its Agent when replay finishes. Keep the shared
	# BrowserSession alive so the continuation Agent can resume from the
	# page state reconstructed by the safe-history replay.
	browser = build_browser_session(headless=headless, keep_alive=True)
	try:
		actions = await _load_and_replay_history(browser=browser, history_path=history_path)
		submit_was_finalized = 'submit_application' in actions

		final_tools, final_controller = build_native_tools()
		final_agent = build_agent(
			browser_session=browser,
			tools=final_tools,
			actions=[done_output()] if submit_was_finalized else [submit_output(), done_output()],
			crash_controller=final_controller,
		)
		await final_agent.run(max_steps=3)
	finally:
		await browser.kill()


async def _run_harness_recovery(
	*,
	server_url: str,
	history_path: Path,
	runtime_db: Path,
	workflow_id: str,
	run_id: str,
	headless: bool,
) -> None:
	# Keep the replayed browser state alive across reader Agent -> continuation Agent.
	browser = build_browser_session(headless=headless, keep_alive=True)
	try:
		await _load_and_replay_history(browser=browser, history_path=history_path)

		with SQLiteRuntimeStorage(runtime_db) as storage:
			recovered = RecoveryBootstrap(storage).restore(workflow_id, run_id)
			states = dict(recovered.states)
			last_effect_seq = recovered.last_effect_seq
			state = states[UNIT_ID]

			if state.status is UnitStatus.UNKNOWN:
				reconciliation = ReconciliationCoordinator(
					storage=storage,
					checkpoint_manager=CheckpointManager(storage),
					workflow_id=workflow_id,
					run_id=recovered.run_id,
					contract=recovered.contract,
					verifier=HttpApplicationStatusVerifier(server_url),
				)
				reconciled = await reconciliation.reconcile(
					unit_id=UNIT_ID,
					states=states,
					last_effect_seq=last_effect_seq,
					context=None,
				)
				states = dict(reconciled.states)
				last_effect_seq = reconciled.record.seq
				state = states[UNIT_ID]

			should_submit = state.status is UnitStatus.ACTIVE and state.effect_status is EffectStatus.NOT_APPLIED

			adapter = BrowserUseRuntimeAdapter(
				contract=recovered.contract,
				verifier=HttpApplicationStatusVerifier(server_url),
				claim_source=NoopClaimSource(),
				states=states,
			)
			coordinator = SideEffectCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=workflow_id,
				run_id=recovered.run_id,
				contract=recovered.contract,
				verifier=HttpApplicationStatusVerifier(server_url),
			)
			tools = Tools()
			register_harness_submit_action(
				tools=tools,
				runtime_adapter=adapter,
				coordinator=coordinator,
				last_effect_seq=last_effect_seq,
				crash_point=None,
				crash_controller=CrashController(),
			)
			final_agent = build_agent(
				browser_session=browser,
				tools=tools,
				actions=[submit_output(), done_output()] if should_submit else [done_output()],
			)

			await final_agent.run(
				max_steps=3,
				on_step_start=adapter.on_step_start,
				on_step_end=adapter.on_step_end,
			)
	finally:
		await browser.kill()


async def run_crash(args: argparse.Namespace) -> None:
	if args.mode is RecoveryMode.NATIVE:
		await _run_native_crash(
			server_url=args.server_url,
			history_path=args.history_file,
			crash_point=args.crash_point,
			headless=args.headless,
		)
		return

	if args.runtime_db is None:
		raise ValueError('--runtime-db is required for harness mode')
	await _run_harness_crash(
		server_url=args.server_url,
		history_path=args.history_file,
		runtime_db=args.runtime_db,
		workflow_id=args.workflow_id,
		run_id=args.run_id,
		crash_point=args.crash_point,
		headless=args.headless,
	)


async def run_recover(args: argparse.Namespace) -> None:
	if args.mode is RecoveryMode.NATIVE:
		await _run_native_recovery(
			server_url=args.server_url,
			history_path=args.history_file,
			headless=args.headless,
		)
		return

	if args.runtime_db is None:
		raise ValueError('--runtime-db is required for harness mode')
	await _run_harness_recovery(
		server_url=args.server_url,
		history_path=args.history_file,
		runtime_db=args.runtime_db,
		workflow_id=args.workflow_id,
		run_id=args.run_id,
		headless=args.headless,
	)


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Native Browser Use history baseline worker.')
	parser.add_argument('--phase', choices=('crash', 'recover'), required=True)
	parser.add_argument('--mode', type=RecoveryMode, choices=list(RecoveryMode), required=True)
	parser.add_argument('--crash-point', type=CrashPoint, choices=list(CrashPoint), required=True)
	parser.add_argument('--server-url', required=True)
	parser.add_argument('--history-file', type=Path, required=True)
	parser.add_argument('--runtime-db', type=Path)
	parser.add_argument('--workflow-id', default='native-history-application-7')
	parser.add_argument('--run-id', default='run-1')
	parser.add_argument('--headless', action=argparse.BooleanOptionalAction, default=True)
	return parser.parse_args()


def main() -> None:
	args = _parse_args()
	try:
		if args.phase == 'crash':
			asyncio.run(run_crash(args))
		else:
			asyncio.run(run_recover(args))
	except HistoryCommitCrash:
		if args.phase != 'crash':
			raise
		raise SystemExit(CRASH_EXIT_CODE)


if __name__ == '__main__':
	main()
