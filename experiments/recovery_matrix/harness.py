"""Recoverable Harness trials for the Day 9 fault-injection matrix."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from browser_use.browser.session import BrowserSession
from browser_use.recovery import (
	BrowserActionBridge,
	BrowserUseRuntimeAdapter,
	ReconciliationCoordinator,
	RecoveryBootstrap,
	SemanticContract,
	SemanticUnit,
	SideEffectCoordinator,
	SideEffectExecutionError,
	SimulatedCrash,
	UnitRuntimeState,
	UnitStatus,
)
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.recovery.verification import VerificationResult, Verifier
from browser_use.tools.service import Tools
from experiments.recovery_matrix.models import ExecutionMode, FaultScenario, TrialResult
from experiments.recovery_matrix.world import fetch_world_state, reset_world, submit_once, wait_for_submit_count
from experiments.recovery_smoke.run_agent import UNIT_ID, NoopClaimSource, build_contract, find_submit_button_by_selector
from experiments.recovery_smoke.verifier import ApplicationStatusVerifier, build_page_status_reader


class UnavailableVerifier:
	"""Observational verifier that deterministically models an evidence outage."""

	observational = True

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		raise ConnectionError('injected verifier outage')


def _crash_after_prepared() -> None:
	raise SimulatedCrash('injected crash after PREPARED')


def _crash_after_attempted() -> None:
	raise SimulatedCrash('injected crash after ATTEMPTED')


async def _assert_safety_gate_blocks(
	*,
	storage: SQLiteRuntimeStorage,
	workflow_id: str,
	run_id: str,
	contract: SemanticContract,
	verifier: Verifier[object],
	states: Mapping[str, UnitRuntimeState],
	last_effect_seq: int,
) -> None:
	coordinator = SideEffectCoordinator(
		storage=storage,
		checkpoint_manager=CheckpointManager(storage),
		workflow_id=workflow_id,
		run_id=run_id,
		contract=contract,
		verifier=verifier,
	)
	executor_calls = 0

	async def forbidden_executor() -> None:
		nonlocal executor_calls
		executor_calls += 1

	try:
		await coordinator.execute(
			unit_id=UNIT_ID,
			effect_key='submit_application',
			states=dict(states),
			last_effect_seq=last_effect_seq,
			executor=forbidden_executor,
		)
	except SideEffectExecutionError:
		pass
	else:
		raise AssertionError('safety gate unexpectedly allowed UNKNOWN effect execution')
	if executor_calls != 0:
		raise AssertionError('safety gate rejected too late: external executor was called')


async def run_harness_trial(
	*,
	scenario: FaultScenario,
	trial: int,
	server_url: str,
	browser_session: BrowserSession,
	runtime_db: Path,
) -> TrialResult:
	"""Run one real-browser crash/recovery trial through the Recoverable Harness."""
	await reset_world(server_url)
	if runtime_db.exists():
		runtime_db.unlink()

	contract = build_contract()
	workflow_id = f'matrix-{scenario.value}-{trial}'
	run1 = 'run-1'
	reconciliation_outcomes: list[str] = []
	notes: list[str] = []
	retry_count = 0
	unsafe_retry_count = 0

	status_verifier = ApplicationStatusVerifier(contract, build_page_status_reader(browser_session))

	with SQLiteRuntimeStorage(runtime_db) as storage:
		storage.start_run(WorkflowRun(workflow_id=workflow_id, run_id=run1))
		storage.save_contract(workflow_id, contract)
		adapter = BrowserUseRuntimeAdapter(
			contract=contract,
			verifier=status_verifier,
			claim_source=NoopClaimSource(),
		)
		adapter.activate(UNIT_ID)
		coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=workflow_id,
			run_id=run1,
			contract=contract,
			verifier=status_verifier,
		)
		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=adapter,
			side_effect_coordinator=coordinator,
			unit_id=UNIT_ID,
			element_finder=find_submit_button_by_selector,
			after_prepared_hook=_crash_after_prepared if scenario is FaultScenario.AFTER_PREPARED else None,
			after_attempt_hook=(
				_crash_after_attempted
				if scenario in {FaultScenario.AFTER_ATTEMPTED, FaultScenario.VERIFIER_UNAVAILABLE}
				else None
			),
		)
		bridge.register()
		page = await browser_session.must_get_current_page()
		await page.goto(server_url)
		try:
			await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)
		except SimulatedCrash:
			pass
		else:
			raise AssertionError('fault scenario did not stop the first run')

		if scenario is FaultScenario.AFTER_PREPARED:
			state = await fetch_world_state(server_url)
			if int(str(state['submit_count'])) != 0:
				raise AssertionError(f'AFTER_PREPARED executed external effect: {state}')
		else:
			state = await wait_for_submit_count(server_url, 1)
			if int(str(state['submit_count'])) != 1:
				raise AssertionError(f'first submit did not land before crash: {state}')

	# Re-open persistence to model a restarted process.
	with SQLiteRuntimeStorage(runtime_db) as storage:
		recovered = RecoveryBootstrap(storage).restore(workflow_id, 'run-2')
		if recovered.states[UNIT_ID].status is not UnitStatus.UNKNOWN:
			raise AssertionError(f'expected UNKNOWN after crash, got {recovered.states[UNIT_ID]}')

		status_verifier = ApplicationStatusVerifier(contract, build_page_status_reader(browser_session))
		await _assert_safety_gate_blocks(
			storage=storage,
			workflow_id=workflow_id,
			run_id=recovered.run_id,
			contract=contract,
			verifier=status_verifier,
			states=recovered.states,
			last_effect_seq=recovered.last_effect_seq,
		)
		notes.append('UNKNOWN safety gate blocked direct re-execution')

		if scenario is FaultScenario.VERIFIER_UNAVAILABLE:
			outage = ReconciliationCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=workflow_id,
				run_id=recovered.run_id,
				contract=contract,
				verifier=UnavailableVerifier(),
			)
			first = await outage.reconcile(
				unit_id=UNIT_ID,
				states=dict(recovered.states),
				last_effect_seq=recovered.last_effect_seq,
				context=None,
			)
			reconciliation_outcomes.append(first.verification_result.outcome.value)
			if first.states[UNIT_ID].status is not UnitStatus.UNKNOWN:
				raise AssertionError('INCONCLUSIVE reconciliation must remain UNKNOWN')
			await _assert_safety_gate_blocks(
				storage=storage,
				workflow_id=workflow_id,
				run_id=recovered.run_id,
				contract=contract,
				verifier=status_verifier,
				states=first.states,
				last_effect_seq=first.record.seq,
			)
			notes.append('verifier outage stayed UNKNOWN and remained blocked')
			states_for_reconcile = dict(first.states)
			last_effect_seq = first.record.seq
		else:
			states_for_reconcile = dict(recovered.states)
			last_effect_seq = recovered.last_effect_seq

		reconciliation = ReconciliationCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=workflow_id,
			run_id=recovered.run_id,
			contract=contract,
			verifier=status_verifier,
		)
		reconciled = await reconciliation.reconcile(
			unit_id=UNIT_ID,
			states=states_for_reconcile,
			last_effect_seq=last_effect_seq,
			context=None,
		)
		reconciliation_outcomes.append(reconciled.verification_result.outcome.value)

		if scenario is FaultScenario.AFTER_PREPARED:
			if reconciled.verification_result.outcome.value != 'rejected':
				raise AssertionError('effect-before-crash absence should reconcile as REJECTED')
			retry_count = 1
			retry_coordinator = SideEffectCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=workflow_id,
				run_id=recovered.run_id,
				contract=contract,
				verifier=status_verifier,
			)

			async def execute_after_rejected() -> dict[str, object]:
				await submit_once(browser_session, server_url, expected_count=1)
				return {'submitted': True, 'recovery_retry': True}

			final = await retry_coordinator.execute(
				unit_id=UNIT_ID,
				effect_key='submit_application',
				states=dict(reconciled.states),
				last_effect_seq=reconciled.record.seq,
				executor=execute_after_rejected,
			)
			final_unit_status = final.states[UNIT_ID].status.value
		else:
			final_unit_status = reconciled.states[UNIT_ID].status.value

	state = await fetch_world_state(server_url)
	final_status = str(state.get('status', 'UNKNOWN'))
	submit_count = int(str(state.get('submit_count', 0)))
	task_completed = final_status == 'SUBMITTED' and final_unit_status == UnitStatus.COMPLETED.value
	duplicate_effect = submit_count > 1
	safe_recovery = task_completed and submit_count == 1 and unsafe_retry_count == 0

	return TrialResult(
		mode=ExecutionMode.HARNESS,
		scenario=scenario,
		trial=trial,
		final_status=final_status,
		submit_count=submit_count,
		retry_count=retry_count,
		unsafe_retry_count=unsafe_retry_count,
		duplicate_effect=duplicate_effect,
		task_completed=task_completed,
		safe_recovery=safe_recovery,
		reconciliation_outcomes=tuple(reconciliation_outcomes),
		notes=tuple(notes),
	)
