"""Day 8 step 3: crash injection between ATTEMPTED and verification, then recovery.

End-to-end flow exercised here:

    submit_application -> PREPARED -> real POST /submit -> ATTEMPTED
        -> [SimulatedCrash]
        -> restart: RecoveryBootstrap -> UNKNOWN/UNKNOWN/INCONCLUSIVE
        -> safety gate rejects a resubmit
        -> ReconciliationCoordinator + real ApplicationStatusVerifier
        -> VERIFIED -> COMPLETED/COMMITTED/VERIFIED
        -> the original effect_id/attempt_id are reused, submit_count stays 1
"""

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from browser_use.actor.element import Element
from browser_use.actor.page import Page
from browser_use.browser.session import BrowserSession
from browser_use.recovery import (
	BrowserActionBridge,
	BrowserUseRuntimeAdapter,
	ConditionSpec,
	EffectSpec,
	EffectStatus,
	Idempotency,
	ReconciliationCoordinator,
	RecoveryBootstrap,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	SideEffectCoordinator,
	SideEffectExecutionError,
	SimulatedCrash,
	TargetSpec,
	UnitIdentity,
	UnitStatus,
	VerificationSource,
	VerificationSpec,
	VerificationStatus,
)
from browser_use.recovery.browser_use_adapter import CompletionClaim
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.recovery.persistence.models import EffectRecordStatus
from browser_use.recovery.verification import VerificationOutcome
from browser_use.tools.service import Tools
from experiments.recovery_smoke import server as smoke_server_module
from experiments.recovery_smoke.verifier import ApplicationStatusVerifier, build_page_status_reader

UNIT_ID = 'u_submit'
WORKFLOW_ID = 'smoke-application-7'


def build_contract() -> SemanticContract:
	"""Build the smoke application Contract."""
	unit = SemanticUnit(
		unit_id=UNIT_ID,
		identity=UnitIdentity(
			intent_key='submit_application',
			target_key='application/7',
			outcome_key='application_submitted',
		),
		intent='submit application #7',
		target=TargetSpec(type='application', key='application/7', attributes={'application_id': '7'}),
		postconditions=(ConditionSpec(description='application #7 exists with submitted status'),),
		effect=EffectSpec(
			has_side_effect=True,
			idempotency=Idempotency.NON_IDEMPOTENT,
			reversibility=Reversibility.UNKNOWN,
		),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='observe the application status page',
		),
	)
	return SemanticContract(contract_id=WORKFLOW_ID, task_id='submit-application-7', version=1, units=(unit,))


class NoopClaimSource:
	"""Never emit a completion claim."""

	async def completion_claim(self, agent: object, unit: SemanticUnit) -> CompletionClaim | None:
		return None


async def find_submit_button(page: Page, llm: object) -> Element:
	"""Locate the submit button deterministically through the real DOM."""
	elements = await page.get_elements_by_css_selector('#submit-btn')
	if not elements:
		raise ValueError('submit button (#submit-btn) not found')
	return elements[0]


@pytest.fixture
async def smoke_server(tmp_path: Path):
	"""Run the real FastAPI application server with a fresh SQLite state file."""
	smoke_server_module._db_path = tmp_path / 'state.db'

	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		port = sock.getsockname()[1]

	config = uvicorn.Config(smoke_server_module.app, host='127.0.0.1', port=port, log_level='warning')
	uvicorn_server = uvicorn.Server(config)
	task = asyncio.create_task(uvicorn_server.serve())

	base_url = f'http://127.0.0.1:{port}'
	async with httpx.AsyncClient() as client:
		deadline = asyncio.get_event_loop().time() + 10
		while True:
			try:
				response = await client.get(f'{base_url}/status')
				response.raise_for_status()
				break
			except Exception:
				if asyncio.get_event_loop().time() >= deadline:
					uvicorn_server.should_exit = True
					task.cancel()
					raise
				await asyncio.sleep(0.1)

	yield base_url

	uvicorn_server.should_exit = True
	try:
		await asyncio.wait_for(task, timeout=10)
	except (asyncio.CancelledError, TimeoutError):
		pass


async def fetch_status(base_url: str) -> dict[str, object]:
	"""Return the external application state as JSON."""
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{base_url}/status')
		response.raise_for_status()
		return response.json()


async def wait_for_submitted(base_url: str, timeout: float = 10.0) -> dict[str, object]:
	"""Poll the external state until the browser's POST has landed."""
	deadline = asyncio.get_event_loop().time() + timeout
	while True:
		status = await fetch_status(base_url)
		if status.get('status') == 'SUBMITTED':
			return status
		if asyncio.get_event_loop().time() >= deadline:
			return status
		await asyncio.sleep(0.25)


@pytest.mark.asyncio
async def test_crash_between_attempted_and_verification_recovers_via_reconciliation(
	tmp_path: Path,
	smoke_server: str,
	browser_session: BrowserSession,
) -> None:
	contract = build_contract()
	storage_path = tmp_path / 'runtime.db'

	# ---- phase 1: submit through the harness with a crash hook ----
	with SQLiteRuntimeStorage(storage_path) as storage:
		storage.start_run(WorkflowRun(workflow_id=WORKFLOW_ID, run_id='run-1'))
		storage.save_contract(WORKFLOW_ID, contract)

		status_verifier = ApplicationStatusVerifier(contract, build_page_status_reader(browser_session))
		adapter = BrowserUseRuntimeAdapter(
			contract=contract,
			verifier=status_verifier,
			claim_source=NoopClaimSource(),
		)
		adapter.activate(UNIT_ID)

		coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=WORKFLOW_ID,
			run_id='run-1',
			contract=contract,
			verifier=status_verifier,
			effect_id_factory=lambda: 'effect-1',
			attempt_id_factory=lambda: 'attempt-1',
		)

		def crash_here() -> None:
			raise SimulatedCrash('simulated process crash after ATTEMPTED')

		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=adapter,
			side_effect_coordinator=coordinator,
			unit_id=UNIT_ID,
			element_finder=find_submit_button,
			after_attempt_hook=crash_here,
		)
		bridge.register()

		page = await browser_session.must_get_current_page()
		await page.goto(smoke_server)

		with pytest.raises(SimulatedCrash):
			await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)

		# Ledger is open: PREPARED + ATTEMPTED with no closing record.
		records = storage.read_effects(WORKFLOW_ID)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
		]
		assert records[0].effect_id == 'effect-1'
		assert records[0].attempt_id == 'attempt-1'
		assert records[1].effect_id == 'effect-1'
		assert records[1].attempt_id == 'attempt-1'
		assert storage.load_latest_checkpoint(WORKFLOW_ID).last_effect_seq == records[0].seq  # type: ignore[union-attr]

	# ---- phase 2: restart from SQLite ----
	with SQLiteRuntimeStorage(storage_path) as storage:
		recovered = RecoveryBootstrap(storage).restore(WORKFLOW_ID, 'run-2')

		assert recovered.states[UNIT_ID].status is UnitStatus.UNKNOWN
		assert recovered.states[UNIT_ID].effect_status is EffectStatus.UNKNOWN
		assert recovered.states[UNIT_ID].verification_status is VerificationStatus.INCONCLUSIVE

		# The world already remembers the submission.
		external = await wait_for_submitted(smoke_server)
		assert external['status'] == 'SUBMITTED'
		assert external['submit_count'] == 1

		status_verifier = ApplicationStatusVerifier(contract, build_page_status_reader(browser_session))

		# Safety gate: resubmitting under UNKNOWN is rejected before any click.
		gate_coordinator = SideEffectCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=WORKFLOW_ID,
			run_id='run-2',
			contract=contract,
			verifier=status_verifier,
		)
		executor_calls = 0

		async def resubmit_executor() -> dict[str, object]:
			nonlocal executor_calls
			executor_calls += 1
			return {'resubmitted': True}

		with pytest.raises(SideEffectExecutionError, match='requires ACTIVE status'):
			await gate_coordinator.execute(
				unit_id=UNIT_ID,
				effect_key='submit_application',
				states=dict(recovered.states),
				last_effect_seq=recovered.last_effect_seq,
				executor=resubmit_executor,
			)
		assert executor_calls == 0
		assert (await fetch_status(smoke_server))['submit_count'] == 1

		# Reconciliation observes the real application page and closes the old attempt.
		reconciliation = ReconciliationCoordinator(
			storage=storage,
			checkpoint_manager=CheckpointManager(storage),
			workflow_id=WORKFLOW_ID,
			run_id='run-2',
			contract=contract,
			verifier=status_verifier,
		)
		result = await reconciliation.reconcile(
			unit_id=UNIT_ID,
			states=dict(recovered.states),
			last_effect_seq=recovered.last_effect_seq,
			context=None,
		)

		assert result.verification_result.outcome is VerificationOutcome.VERIFIED
		assert result.states[UNIT_ID].status is UnitStatus.COMPLETED
		assert result.states[UNIT_ID].effect_status is EffectStatus.COMMITTED
		assert result.states[UNIT_ID].verification_status is VerificationStatus.VERIFIED
		assert result.record.effect_id == 'effect-1'
		assert result.record.attempt_id == 'attempt-1'

		final_records = storage.read_effects(WORKFLOW_ID)
		assert [record.status for record in final_records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.COMMITTED,
		]
		assert {record.effect_id for record in final_records} == {'effect-1'}
		assert {record.attempt_id for record in final_records} == {'attempt-1'}
		assert final_records[-1].seq == result.record.seq

		# Exactly one submission ever reached the external world.
		assert (await fetch_status(smoke_server))['submit_count'] == 1
