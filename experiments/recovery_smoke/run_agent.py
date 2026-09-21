"""Wire the Day 8 smoke: ``submit_application`` through the side-effect boundary.

Direct mode (default) executes the registered ``submit_application`` action
through the Browser Use Tools registry without an LLM, proving the durable
ledger records PREPARED -> ATTEMPTED -> final for one real browser click.

Crash mode simulates the agent process dying right after the click and the
ATTEMPTED ledger fact, before verification:

    uv run python experiments/recovery_smoke/run_agent.py --url ... --crash-after-attempt

Recover mode simulates a restarted process: bootstrap the Runtime from SQLite
(the unit must come back UNKNOWN), prove the safety gate blocks a resubmit,
then reconcile the old attempt through the real application status page:

    uv run python experiments/recovery_smoke/run_agent.py --url ... --recover

Agent mode runs a full Browser Use Agent whose LLM decides to call
``submit_application``.

Prerequisites:

    uv run python experiments/recovery_smoke/server.py --port 8765

Then, direct mode (deterministic, no API key):

    uv run python experiments/recovery_smoke/run_agent.py --url http://127.0.0.1:8765

Agent mode (needs BROWSER_USE_API_KEY or OPENAI_API_KEY):

    uv run python experiments/recovery_smoke/run_agent.py --url http://127.0.0.1:8765 --agent
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from uuid_extensions import uuid7str

from browser_use.actor.element import Element
from browser_use.actor.page import Page
from browser_use.agent.service import Agent
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.llm.base import BaseChatModel
from browser_use.recovery import (
	BrowserActionBridge,
	BrowserUseRuntimeAdapter,
	ConditionSpec,
	EffectSpec,
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
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.browser_use_adapter import CompletionClaim
from browser_use.recovery.persistence import CheckpointManager, SQLiteRuntimeStorage, WorkflowRun
from browser_use.tools.service import Tools
from experiments.recovery_smoke.verifier import ApplicationStatusVerifier, build_page_status_reader

UNIT_ID = 'u_submit'
WORKFLOW_ID = 'smoke-application-7'
HARNESS_DB = Path(__file__).resolve().parent / 'runtime.db'


def build_contract() -> SemanticContract:
	"""Build the one-unit semantic Contract for submitting application #7."""
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
			description='POST /submit on the local application page',
		),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='observe the application status page and confirm Status: SUBMITTED',
		),
	)
	return SemanticContract(contract_id=WORKFLOW_ID, task_id='submit-application-7', version=1, units=(unit,))


class NoopClaimSource:
	"""Never emit a completion claim; the side-effect boundary owns completion."""

	async def completion_claim(self, agent: Agent, unit: SemanticUnit) -> CompletionClaim | None:
		return None


async def find_submit_button_by_selector(page: Page, llm: BaseChatModel | None) -> Element:
	"""Deterministic element finder used when no LLM is configured."""
	elements = await page.get_elements_by_css_selector('#submit-btn')
	if not elements:
		raise ValueError('submit application button (#submit-btn) not found on the page')
	return elements[0]


def build_llm(model: str | None) -> BaseChatModel | None:
	"""Return the configured LLM, preferring ChatBrowserUse when a key exists."""
	if model:
		from browser_use import ChatOpenAI

		return ChatOpenAI(model=model)
	if os.environ.get('BROWSER_USE_API_KEY'):
		from browser_use import ChatBrowserUse

		return ChatBrowserUse()
	if os.environ.get('OPENAI_API_KEY'):
		from browser_use import ChatOpenAI

		return ChatOpenAI(model=os.environ.get('SMOKE_OPENAI_MODEL', 'gpt-4.1-mini'))
	return None


def crash_here() -> None:
	"""Fault injection: die inside the ATTEMPTED -> verification window."""
	raise SimulatedCrash('simulated process crash after ATTEMPTED was persisted')


async def fetch_status(server_url: str) -> dict[str, object]:
	"""Return the external application state as JSON."""
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{server_url}/status')
		response.raise_for_status()
		return response.json()


async def wait_for_status(server_url: str, expected_status: str, timeout: float = 15.0) -> dict[str, object]:
	"""Poll the external state until the submission has landed in the world."""
	deadline = asyncio.get_event_loop().time() + timeout
	while True:
		status = await fetch_status(server_url)
		if status.get('status') == expected_status:
			return status
		if asyncio.get_event_loop().time() >= deadline:
			return status
		await asyncio.sleep(0.25)


def print_ledger(storage: SQLiteRuntimeStorage, workflow_id: str) -> None:
	"""Print the durable effect ledger."""
	print('Effect ledger:')
	for record in storage.read_effects(workflow_id):
		print(f'  seq={record.seq} {record.status.value} effect={record.effect_id} attempt={record.attempt_id}')


async def _start_browser(headless: bool) -> BrowserSession:
	browser_session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=headless,
			user_data_dir=None,
			keep_alive=True,
		)
	)
	await browser_session.start()
	return browser_session


async def run_smoke(
	server_url: str,
	*,
	headless: bool,
	agent_mode: bool,
	model: str | None,
	crash_after_attempt: bool = False,
) -> None:
	contract = build_contract()
	run_id = uuid7str()

	browser_session = await _start_browser(headless)

	try:
		with SQLiteRuntimeStorage(HARNESS_DB) as storage:
			storage.start_run(WorkflowRun(workflow_id=WORKFLOW_ID, run_id=run_id))
			storage.save_contract(WORKFLOW_ID, contract)

			llm = build_llm(model)

			status_verifier = ApplicationStatusVerifier(
				contract,
				build_page_status_reader(browser_session),
			)
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
				run_id=run_id,
				contract=contract,
				verifier=status_verifier,
			)

			tools = Tools()
			bridge = BrowserActionBridge(
				tools=tools,
				runtime_adapter=adapter,
				side_effect_coordinator=coordinator,
				last_effect_seq=0,
				llm=llm,
				unit_id=UNIT_ID,
				element_finder=None if llm is not None else find_submit_button_by_selector,
				after_attempt_hook=crash_here if crash_after_attempt else None,
			)
			bridge.register()

			page = await browser_session.must_get_current_page()
			await page.goto(server_url)

			if agent_mode:
				if llm is None:
					raise SystemExit('agent mode requires BROWSER_USE_API_KEY or OPENAI_API_KEY')
				agent = Agent(
					task=(f'Go to {server_url}, then submit the application using the submit_application action exactly once.'),
					llm=llm,
					tools=tools,
					browser_session=browser_session,
				)
				await agent.run()
			elif crash_after_attempt:
				try:
					await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)
				except SimulatedCrash:
					print('CRASH: process died after ATTEMPTED was persisted, before verification')
					print_ledger(storage, WORKFLOW_ID)
					print('External application state:', await wait_for_status(server_url, 'SUBMITTED'))
					print('(world remembers the submission; restart with --recover to reconcile)')
					raise SystemExit(1) from None
			else:
				result = await tools.registry.execute_action(
					'submit_application',
					params={},
					browser_session=browser_session,
				)
				print('ActionResult:', result.extracted_content)

			print()
			print_ledger(storage, WORKFLOW_ID)
			print('Runtime states:', {unit_id: state.status.value for unit_id, state in adapter.states.items()})
			print('bridge last_effect_seq:', bridge.last_effect_seq)

			async with httpx.AsyncClient() as client:
				response = await client.get(f'{server_url}/status')
				response.raise_for_status()
				print('External application state:', response.json())
	finally:
		await browser_session.kill()


async def run_recover(server_url: str, *, headless: bool) -> None:
	"""Restart a crashed Runtime: bootstrap UNKNOWN, block resubmit, reconcile."""
	contract = build_contract()
	browser_session = await _start_browser(headless)

	try:
		with SQLiteRuntimeStorage(HARNESS_DB) as storage:
			try:
				recovered = RecoveryBootstrap(storage).restore(WORKFLOW_ID, uuid7str())
			except Exception as exc:
				raise SystemExit(
					f'no crashed runtime to recover ({type(exc).__name__}: {exc}); run --crash-after-attempt first'
				) from exc

			state = recovered.states[UNIT_ID]
			print(
				'Recovered state: '
				f'status={state.status.value} effect={state.effect_status.value} '
				f'verification={state.verification_status.value}'
			)
			print(f'  last_effect_seq={recovered.last_effect_seq} resumed_from={recovered.resumed_from_checkpoint_id}')

			status_verifier = ApplicationStatusVerifier(contract, build_page_status_reader(browser_session))
			page = await browser_session.must_get_current_page()
			await page.goto(server_url)

			# Safety gate: the UNKNOWN state must forbid a resubmit.
			coordinator = SideEffectCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=WORKFLOW_ID,
				run_id=recovered.run_id,
				contract=contract,
				verifier=status_verifier,
			)

			async def never_runs_executor() -> dict[str, object]:
				return {'submitted': True}

			try:
				await coordinator.execute(
					unit_id=UNIT_ID,
					effect_key='submit_application',
					states=dict(recovered.states),
					last_effect_seq=recovered.last_effect_seq,
					executor=never_runs_executor,
				)
			except SideEffectExecutionError as exc:
				print(f'Safety gate blocked resubmit: {exc}')
			else:
				raise SystemExit('BUG: safety gate unexpectedly allowed a resubmit')
			print('External state after blocked resubmit:', await fetch_status(server_url))

			# Reconcile the old attempt through the real application status page.
			reconciliation = ReconciliationCoordinator(
				storage=storage,
				checkpoint_manager=CheckpointManager(storage),
				workflow_id=WORKFLOW_ID,
				run_id=recovered.run_id,
				contract=contract,
				verifier=status_verifier,
			)
			result = await reconciliation.reconcile(
				unit_id=UNIT_ID,
				states=dict(recovered.states),
				last_effect_seq=recovered.last_effect_seq,
				context=None,
			)

			reconciled_state = result.states[UNIT_ID]
			print(f'Reconciliation outcome: {result.verification_result.outcome.value}')
			print(
				'Reconciled state: '
				f'status={reconciled_state.status.value} effect={reconciled_state.effect_status.value} '
				f'verification={reconciled_state.verification_status.value}'
			)
			print(
				'Closing record: '
				f'{result.record.status.value} effect={result.record.effect_id} attempt={result.record.attempt_id}'
			)
			print_ledger(storage, WORKFLOW_ID)
			print('External application state:', await fetch_status(server_url))
	finally:
		await browser_session.kill()


def main() -> None:
	parser = argparse.ArgumentParser(description='Run the recovery smoke submit flow.')
	parser.add_argument('--url', default='http://127.0.0.1:8765', help='Smoke application server URL')
	parser.add_argument('--headless', action='store_true', help='Run the browser headless')
	parser.add_argument('--agent', action='store_true', help='Run a full Agent instead of the direct tools path')
	parser.add_argument('--model', default=None, help='OpenAI model name for the direct/agent path')
	parser.add_argument(
		'--crash-after-attempt',
		action='store_true',
		help='Simulate a process crash right after the ATTEMPTED ledger fact',
	)
	parser.add_argument(
		'--recover',
		action='store_true',
		help='Restart from SQLite: bootstrap UNKNOWN, block resubmit, reconcile the old attempt',
	)
	args = parser.parse_args()

	if args.recover:
		asyncio.run(run_recover(args.url, headless=args.headless))
	elif args.crash_after_attempt:
		if args.agent:
			raise SystemExit('--crash-after-attempt only applies to the direct tools path')
		asyncio.run(
			run_smoke(
				args.url,
				headless=args.headless,
				agent_mode=False,
				model=args.model,
				crash_after_attempt=True,
			)
		)
	else:
		asyncio.run(run_smoke(args.url, headless=args.headless, agent_mode=args.agent, model=args.model))


if __name__ == '__main__':
	main()
