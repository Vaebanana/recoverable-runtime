import asyncio
from pathlib import Path

import pytest

from browser_use.actor.element import Element
from browser_use.actor.page import Page
from browser_use.agent.views import ActionResult
from browser_use.browser.session import BrowserSession
from browser_use.recovery.browser_action_bridge import BrowserActionBridge, BrowserActionBridgeError
from browser_use.recovery.browser_use_adapter import BrowserUseRuntimeAdapter, CompletionClaim
from browser_use.recovery.contracts import (
	EffectSpec,
	EffectStatus,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	UnitRuntimeState,
	UnitStatus,
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.persistence.checkpoint import CheckpointManager
from browser_use.recovery.persistence.models import EffectRecordStatus, WorkflowRun
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.side_effects import SideEffectCoordinator
from browser_use.recovery.verification import (
	VerificationEvidence,
	VerificationOutcome,
	VerificationResult,
	expected_target_evidence,
)
from browser_use.tools.service import Tools


def submit_unit(unit_id: str) -> SemanticUnit:
	"""Build one side-effecting application-submit unit."""
	return SemanticUnit(
		unit_id=unit_id,
		identity=UnitIdentity(
			intent_key='submit_application',
			target_key='application/7',
			outcome_key='application_submitted',
		),
		intent='submit application #7',
		target=TargetSpec(type='application', key='application/7', attributes={'application_id': '7'}),
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


def submit_contract(*units: SemanticUnit) -> SemanticContract:
	"""Build a one-or-more-unit Contract snapshot."""
	return SemanticContract(contract_id='c1', task_id='task-1', version=1, units=units)


class StaticVerifier:
	"""Return one deterministic observational result to the real verification manager."""

	observational = True

	def __init__(self, outcome: VerificationOutcome) -> None:
		self._outcome = outcome

	async def verify(self, unit: SemanticUnit, context: object) -> VerificationResult:
		observed = (
			{
				'application_id': unit.target.attributes['application_id'],
				'application.status': 'SUBMITTED',
			}
			if self._outcome is VerificationOutcome.VERIFIED
			else {}
		)
		return VerificationResult(
			outcome=self._outcome,
			evidence=VerificationEvidence(
				expected=expected_target_evidence(unit),
				observed=observed,
				source=VerificationSource.BROWSER,
				summary='deterministic test evidence',
			),
		)


class StaticClaimSource:
	"""Never emit a completion claim."""

	async def completion_claim(self, agent: object, unit: SemanticUnit) -> CompletionClaim | None:
		return None


def make_adapter(contract: SemanticContract) -> BrowserUseRuntimeAdapter:
	"""Build an adapter with deterministic collaborators."""
	return BrowserUseRuntimeAdapter(
		contract=contract,
		verifier=StaticVerifier(VerificationOutcome.VERIFIED),
		claim_source=StaticClaimSource(),
	)


def make_coordinator(
	storage: SQLiteRuntimeStorage,
	contract: SemanticContract,
	outcome: VerificationOutcome = VerificationOutcome.VERIFIED,
) -> SideEffectCoordinator:
	"""Build a deterministic coordinator for one workflow run."""
	return SideEffectCoordinator(
		storage=storage,
		checkpoint_manager=CheckpointManager(storage),
		workflow_id='wf-1',
		run_id='run-1',
		contract=contract,
		verifier=StaticVerifier(outcome),
		effect_id_factory=lambda: 'effect-1',
		attempt_id_factory=lambda: 'attempt-1',
	)


async def find_submit_button(page: Page, llm: object) -> Element:
	"""Locate the submit button deterministically through the real DOM."""
	elements = await page.get_elements_by_css_selector('#submit-btn')
	if not elements:
		raise ValueError('submit button (#submit-btn) not found')
	return elements[0]


async def wait_for_post_requests(httpserver, expected_count: int, timeout: float = 10.0) -> int:
	"""Poll the server log until the browser's form POST has arrived.

	``Element.click`` returns before the browser process has finished issuing
	the form submission, so the POST may land a few hundred milliseconds later.
	"""
	deadline = asyncio.get_event_loop().time() + timeout
	while True:
		count = sum(1 for request, _ in httpserver.log if request.method == 'POST')
		if count >= expected_count:
			return count
		if asyncio.get_event_loop().time() >= deadline:
			return count
		await asyncio.sleep(0.25)


NOT_SUBMITTED_HTML = """
<!DOCTYPE html>
<html>
<head><title>Application #7</title></head>
<body>
	<h1>Application #7</h1>
	<p id="status">Status: NOT_SUBMITTED</p>
	<p id="submit-count">Submit count: 0</p>
	<form method="post" action="/submit">
		<button type="submit" id="submit-btn">Submit Application</button>
	</form>
</body>
</html>
"""

SUBMITTED_HTML = """
<!DOCTYPE html>
<html>
<head><title>Application #7</title></head>
<body>
	<h1>Application #7</h1>
	<p id="status">Status: SUBMITTED</p>
	<p id="submit-count">Submit count: 1</p>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_apply_runtime_states_accepts_valid_snapshot_and_rejects_multiple_active() -> None:
	contract = submit_contract(submit_unit('u1'), submit_unit('u2'))
	adapter = make_adapter(contract)

	valid = {
		'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE),
		'u2': UnitRuntimeState(unit_id='u2', status=UnitStatus.PENDING),
	}
	adapter.apply_runtime_states(valid)
	assert adapter.states['u1'].status is UnitStatus.ACTIVE

	invalid = {
		'u1': UnitRuntimeState(unit_id='u1', status=UnitStatus.ACTIVE),
		'u2': UnitRuntimeState(unit_id='u2', status=UnitStatus.ACTIVE),
	}
	with pytest.raises(Exception, match='multiple ACTIVE units'):
		adapter.apply_runtime_states(invalid)
	assert adapter.states['u2'].status is UnitStatus.PENDING


@pytest.mark.asyncio
async def test_register_adds_action_to_tools_and_rejects_double_registration(tmp_path: Path) -> None:
	contract = submit_contract(submit_unit('u_submit'))
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=make_adapter(contract),
			side_effect_coordinator=make_coordinator(storage, contract),
		)

		bridge.register()

		assert 'submit_application' in tools.registry.registry.actions
		with pytest.raises(BrowserActionBridgeError, match='already registered'):
			bridge.register()


@pytest.mark.asyncio
async def test_non_active_unit_is_rejected_before_click_or_ledger_writes(
	tmp_path: Path,
	httpserver,
	browser_session: BrowserSession,
) -> None:
	httpserver.expect_request('/').respond_with_data(NOT_SUBMITTED_HTML, content_type='text/html')
	httpserver.expect_request('/submit', method='POST').respond_with_data(SUBMITTED_HTML, content_type='text/html')

	contract = submit_contract(submit_unit('u_submit'))
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		adapter = make_adapter(contract)
		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=adapter,
			side_effect_coordinator=make_coordinator(storage, contract),
			unit_id='u_submit',
			element_finder=find_submit_button,
		)
		bridge.register()

		page = await browser_session.must_get_current_page()
		await page.goto(httpserver.url_for('/'))

		with pytest.raises(RuntimeError, match='requires ACTIVE status'):
			await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)

		assert storage.read_effects('wf-1') == ()
		assert adapter.states['u_submit'].status is UnitStatus.PENDING
		assert bridge.last_effect_seq == 0
		assert all(request.method != 'POST' for request, _ in httpserver.log)


@pytest.mark.asyncio
async def test_submit_action_flows_through_side_effect_boundary_and_commits(
	tmp_path: Path,
	httpserver,
	browser_session: BrowserSession,
) -> None:
	httpserver.expect_request('/').respond_with_data(NOT_SUBMITTED_HTML, content_type='text/html')
	httpserver.expect_request('/submit', method='POST').respond_with_data(SUBMITTED_HTML, content_type='text/html')

	contract = submit_contract(submit_unit('u_submit'))
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		adapter = make_adapter(contract)
		adapter.activate('u_submit')
		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=adapter,
			side_effect_coordinator=make_coordinator(storage, contract),
			unit_id='u_submit',
			element_finder=find_submit_button,
		)
		bridge.register()

		page = await browser_session.must_get_current_page()
		await page.goto(httpserver.url_for('/'))

		result = await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)

		assert isinstance(result, ActionResult)
		assert 'verification=verified' in (result.extracted_content or '')

		records = storage.read_effects('wf-1')
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.COMMITTED,
		]
		assert records[-1].unit_id == 'u_submit'
		assert records[-1].effect_key == 'submit_application'

		assert adapter.states['u_submit'].status is UnitStatus.COMPLETED
		assert adapter.states['u_submit'].effect_status is EffectStatus.COMMITTED
		assert bridge.last_effect_seq == records[-1].seq

		checkpoint = storage.load_latest_checkpoint('wf-1')
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == records[-1].seq
		assert checkpoint.unit_states['u_submit'] == adapter.states['u_submit']

		assert await wait_for_post_requests(httpserver, expected_count=1) == 1


@pytest.mark.asyncio
async def test_inconclusive_verification_leaves_unit_unknown_and_blocks_resubmit(
	tmp_path: Path,
	httpserver,
	browser_session: BrowserSession,
) -> None:
	httpserver.expect_request('/').respond_with_data(NOT_SUBMITTED_HTML, content_type='text/html')
	httpserver.expect_request('/submit', method='POST').respond_with_data(SUBMITTED_HTML, content_type='text/html')

	contract = submit_contract(submit_unit('u_submit'))
	with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
		storage.start_run(WorkflowRun(workflow_id='wf-1', run_id='run-1'))
		storage.save_contract('wf-1', contract)

		adapter = make_adapter(contract)
		adapter.activate('u_submit')
		tools = Tools()
		bridge = BrowserActionBridge(
			tools=tools,
			runtime_adapter=adapter,
			side_effect_coordinator=make_coordinator(storage, contract, VerificationOutcome.INCONCLUSIVE),
			unit_id='u_submit',
			element_finder=find_submit_button,
		)
		bridge.register()

		page = await browser_session.must_get_current_page()
		await page.goto(httpserver.url_for('/'))

		result = await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)

		assert 'verification=inconclusive' in (result.extracted_content or '')
		records = storage.read_effects('wf-1')
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.UNKNOWN,
		]
		assert adapter.states['u_submit'].status is UnitStatus.UNKNOWN

		# A second attempt is blocked: UNKNOWN state is not re-executable.
		with pytest.raises(RuntimeError, match='requires ACTIVE status'):
			await tools.registry.execute_action('submit_application', params={}, browser_session=browser_session)

		# The first attempt's POST may still be in flight after the blocked call;
		# wait for it and confirm exactly one submission ever reached the server.
		assert await wait_for_post_requests(httpserver, expected_count=1) == 1
		await asyncio.sleep(0.5)
		assert sum(1 for request, _ in httpserver.log if request.method == 'POST') == 1
