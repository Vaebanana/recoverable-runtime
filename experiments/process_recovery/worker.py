"""Independent process worker for Day 10 hard-crash recovery validation.

The crash phase persists Runtime state, enters one configured crash window, and
terminates via ``os._exit``. Recovery phases start in fresh Python processes and
receive only durable identifiers plus the SQLite path; they reconstruct the
Contract and Runtime state through ``RecoveryBootstrap``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from browser_use.recovery import (
	EffectStatus,
	ReconciliationCoordinator,
	RecoveryBootstrap,
	SideEffectCoordinator,
	SideEffectExecutionError,
	UnitStatus,
	VerificationStatus,
)
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.recovery.runtime_state import RuntimeStateManager
from experiments.process_recovery.scenario import (
	EXIT_AFTER_ATTEMPTED,
	EXIT_AFTER_PREPARED,
	UNIT_ID,
	HttpApplicationStatusVerifier,
	ProcessScenario,
	UnavailableVerifier,
	build_contract,
	submit_application,
)


def _hard_exit(code: int) -> None:
	"""Terminate immediately without stack unwinding or context-manager cleanup."""
	os._exit(code)


async def _assert_safety_gate_blocks(
	*,
	storage: SQLiteRuntimeStorage,
	workflow_id: str,
	run_id: str,
	contract,
	states,
	last_effect_seq: int,
	server_url: str,
) -> None:
	"""Prove UNKNOWN cannot reach the real POST /submit executor."""
	coordinator = SideEffectCoordinator(
		storage=storage,
		checkpoint_manager=CheckpointManager(storage),
		workflow_id=workflow_id,
		run_id=run_id,
		contract=contract,
		verifier=HttpApplicationStatusVerifier(server_url),
	)

	async def dangerous_resubmit() -> dict[str, object]:
		return await submit_application(server_url)

	try:
		await coordinator.execute(
			unit_id=UNIT_ID,
			effect_key='submit_application',
			states=dict(states),
			last_effect_seq=last_effect_seq,
			executor=dangerous_resubmit,
		)
	except SideEffectExecutionError:
		return
	raise AssertionError('safety gate unexpectedly allowed UNKNOWN effect execution')


async def run_crash_phase(
	*,
	scenario: ProcessScenario,
	runtime_db: Path,
	server_url: str,
	workflow_id: str,
	run_id: str,
) -> None:
	"""Start a fresh workflow and terminate at the requested durable crash window."""
	contract = build_contract()
	state_manager = RuntimeStateManager()
	states = state_manager.initialize(contract)
	states = state_manager.activate(contract, states, UNIT_ID)

	with SQLiteRuntimeStorage(runtime_db) as storage:
		storage.start_run(WorkflowRun(workflow_id=workflow_id, run_id=run_id))
		storage.save_contract(workflow_id, contract)
		coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=workflow_id,
			run_id=run_id,
			contract=contract,
			verifier=HttpApplicationStatusVerifier(server_url),
		)

		def after_prepared() -> None:
			_hard_exit(EXIT_AFTER_PREPARED)

		def after_attempted() -> None:
			_hard_exit(EXIT_AFTER_ATTEMPTED)

		await coordinator.execute(
			unit_id=UNIT_ID,
			effect_key='submit_application',
			states=states,
			last_effect_seq=0,
			executor=lambda: submit_application(server_url),
			after_prepared_hook=after_prepared if scenario is ProcessScenario.AFTER_PREPARED else None,
			after_attempt_hook=(
				after_attempted if scenario in {ProcessScenario.AFTER_ATTEMPTED, ProcessScenario.VERIFIER_UNAVAILABLE} else None
			),
		)

	raise AssertionError('crash phase returned without terminating the process')


async def run_recovery_phase(
	*,
	scenario: ProcessScenario,
	runtime_db: Path,
	server_url: str,
	workflow_id: str,
	run_id: str,
	inconclusive_only: bool,
) -> None:
	"""Restore only from SQLite, enforce the safety gate, then reconcile."""
	with SQLiteRuntimeStorage(runtime_db) as storage:
		recovered = RecoveryBootstrap(storage).restore(workflow_id, run_id)
		state = recovered.states[UNIT_ID]
		if (
			state.status is not UnitStatus.UNKNOWN
			or state.effect_status is not EffectStatus.UNKNOWN
			or state.verification_status is not VerificationStatus.INCONCLUSIVE
		):
			raise AssertionError(f'expected UNKNOWN/UNKNOWN/INCONCLUSIVE after restart, got {state}')

		contract = recovered.contract
		await _assert_safety_gate_blocks(
			storage=storage,
			workflow_id=workflow_id,
			run_id=recovered.run_id,
			contract=contract,
			states=recovered.states,
			last_effect_seq=recovered.last_effect_seq,
			server_url=server_url,
		)

		verifier = UnavailableVerifier() if inconclusive_only else HttpApplicationStatusVerifier(server_url)
		reconciliation = ReconciliationCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=workflow_id,
			run_id=recovered.run_id,
			contract=contract,
			verifier=verifier,
		)
		reconciled = await reconciliation.reconcile(
			unit_id=UNIT_ID,
			states=dict(recovered.states),
			last_effect_seq=recovered.last_effect_seq,
			context=None,
		)

		if inconclusive_only:
			state = reconciled.states[UNIT_ID]
			if (
				state.status is not UnitStatus.UNKNOWN
				or state.effect_status is not EffectStatus.UNKNOWN
				or state.verification_status is not VerificationStatus.INCONCLUSIVE
			):
				raise AssertionError('INCONCLUSIVE reconciliation must remain durably UNKNOWN')
			return

		if scenario is ProcessScenario.AFTER_PREPARED:
			if reconciled.verification_result.outcome.value != 'rejected':
				raise AssertionError('AFTER_PREPARED must reconcile the untouched world as REJECTED')
			retry = SideEffectCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=workflow_id,
				run_id=recovered.run_id,
				contract=contract,
				verifier=HttpApplicationStatusVerifier(server_url),
			)
			final = await retry.execute(
				unit_id=UNIT_ID,
				effect_key='submit_application',
				states=dict(reconciled.states),
				last_effect_seq=reconciled.record.seq,
				executor=lambda: submit_application(server_url),
			)
			if final.states[UNIT_ID].status is not UnitStatus.COMPLETED:
				raise AssertionError(f'retry after NOT_APPLIED did not complete: {final.states[UNIT_ID]}')
			return

		if reconciled.states[UNIT_ID].status is not UnitStatus.COMPLETED:
			raise AssertionError(f'reconciliation did not complete the original attempt: {reconciled.states[UNIT_ID]}')


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Run one process-level recovery worker phase.')
	parser.add_argument('--phase', choices=('crash', 'recover', 'recover-inconclusive'), required=True)
	parser.add_argument('--scenario', type=ProcessScenario, choices=list(ProcessScenario), required=True)
	parser.add_argument('--runtime-db', type=Path, required=True)
	parser.add_argument('--server-url', required=True)
	parser.add_argument('--workflow-id', required=True)
	parser.add_argument('--run-id', required=True)
	return parser.parse_args()


def main() -> None:
	args = _parse_args()
	if args.phase == 'crash':
		asyncio.run(
			run_crash_phase(
				scenario=args.scenario,
				runtime_db=args.runtime_db,
				server_url=args.server_url,
				workflow_id=args.workflow_id,
				run_id=args.run_id,
			)
		)
		return

	asyncio.run(
		run_recovery_phase(
			scenario=args.scenario,
			runtime_db=args.runtime_db,
			server_url=args.server_url,
			workflow_id=args.workflow_id,
			run_id=args.run_id,
			inconclusive_only=args.phase == 'recover-inconclusive',
		)
	)


if __name__ == '__main__':
	main()
