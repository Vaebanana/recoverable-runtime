"""Real Agent actions pass through the generic durable harness."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel
from werkzeug.wrappers import Response

from browser_use import BrowserProfile
from browser_use.agent.service import Agent
from browser_use.browser.session import BrowserSession
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.recovery.browser_use_adapter import RuntimeProgressBlockedError
from browser_use.recovery.contracts import (
	ConditionSpec,
	EffectSpec,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	UnitStatus,
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.harness import RecoverableHarness
from browser_use.recovery.observational_verifier import (
	BrowserObservationSource,
	BrowserObservationVerifier,
	ObservationEvidence,
	ObservationSnapshot,
)
from browser_use.recovery.persistence.models import EffectRecordDraft, EffectRecordStatus
from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from browser_use.recovery.side_effects import SimulatedCrash
from experiments.native_history_baseline.scripted_llm import ScriptedLLM, done_output, navigate_output


class PageInterpreter:
	def __init__(self, *, allow_submit_evidence: bool = True) -> None:
		self.allow_submit_evidence = allow_submit_evidence

	async def extract(self, unit: SemanticUnit, snapshot: ObservationSnapshot) -> ObservationEvidence:
		if unit.unit_id == 'u_submit':
			if not self.allow_submit_evidence:
				return ObservationEvidence(target_quote='Application #7', condition_quotes=('',))
			positive = 'Status: SUBMITTED' if 'Status: SUBMITTED' in snapshot.text else ''
			negative = 'Status: NOT_SUBMITTED' if 'Status: NOT_SUBMITTED' in snapshot.text else None
			status_quote = positive or negative or ''
			record = next(
				(line for line in snapshot.text.splitlines() if 'Application #7' in line and status_quote in line),
				'',
			)
			return ObservationEvidence(
				record_quote=record,
				target_quote='Application #7',
				condition_quotes=(positive,),
				negative_quote=negative,
			)
		quote = {
			'u_locate': 'Application #7',
			'u_fill': 'John Doe',
		}[unit.unit_id]
		record = next((line for line in snapshot.text.splitlines() if 'Application #7' in line and quote in line), '')
		return ObservationEvidence(record_quote=record, target_quote='Application #7', condition_quotes=(quote,))


class ClickScript(ScriptedLLM):
	def __init__(self, url: str) -> None:
		first = navigate_output(url)
		first['completion_claim_unit_id'] = 'u_locate'
		super().__init__([first])
		self.url = url
		self.prompts: list[str] = []

	async def ainvoke(self, *args: object, **kwargs: object) -> ChatInvokeCompletion:
		arguments = iter(args)
		messages = str(next(arguments, ''))
		self.prompts.append(messages)
		if self._index in {1, 2}:
			output_format = kwargs.get('output_format') or next(arguments, None)
			if self._index == 1:
				match = re.search(r'\[(\d+)\]<input name=name', messages)
				payload = {
					'evaluation_previous_goal': 'application located',
					'memory': '',
					'next_goal': 'fill form',
					'completion_claim_unit_id': 'u_fill',
					'action': [{'input': {'index': int(match.group(1)), 'text': 'John Doe'}}] if match else [],
				}
			else:
				match = re.search(r'\[(\d+)\]<button type=submit', messages)
				payload = {
					'evaluation_previous_goal': 'form filled',
					'memory': '',
					'next_goal': 'submit',
					'effect_boundary_action_index': 0,
					'action': [
						{'click': {'index': int(match.group(1))}} if match else {},
						{'navigate': {'url': self.url + '/should-not-run'}},
					],
				}
			assert match is not None, messages[-4000:]
			self._index += 1
			model_validate = getattr(output_format, 'model_validate', None)
			assert callable(model_validate)
			completion = model_validate(payload)
			assert isinstance(completion, BaseModel)
			return ChatInvokeCompletion(completion=completion, usage=None)
		return await super().ainvoke(*args, **kwargs)


def contract(observation_url: str | None = None) -> SemanticContract:
	plain = EffectSpec(has_side_effect=False, idempotency=Idempotency.NOT_APPLICABLE, reversibility=Reversibility.NOT_APPLICABLE)
	locate = SemanticUnit(
		unit_id='u_locate',
		identity=UnitIdentity(intent_key='locate_application', target_key='application/7', outcome_key='located'),
		intent='locate application',
		target=TargetSpec(type='application', key='application/7', attributes={'application_id': '7'}),
		postconditions=(ConditionSpec(description='application page is visible', expected_observation='Application #7'),),
		effect=plain,
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='inspect application page'),
	)
	fill = SemanticUnit(
		unit_id='u_fill',
		identity=UnitIdentity(intent_key='fill_form', target_key='application/7', outcome_key='filled'),
		intent='fill form',
		target=locate.target,
		depends_on=('u_locate',),
		postconditions=(ConditionSpec(description='name field contains John Doe', expected_observation='John Doe'),),
		effect=plain,
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='inspect application form'),
	)
	unit = SemanticUnit(
		unit_id='u_submit',
		depends_on=('u_fill',),
		identity=UnitIdentity(intent_key='submit_application', target_key='application/7', outcome_key='submitted'),
		intent='submit application',
		target=TargetSpec(type='application', key='application/7', attributes={'application_id': '7'}),
		postconditions=(
			ConditionSpec(
				description='application status is submitted',
				expected_observation='Status: SUBMITTED',
				negative_observation='Status: NOT_SUBMITTED',
			),
		),
		effect=EffectSpec(has_side_effect=True, idempotency=Idempotency.NON_IDEMPOTENT, reversibility=Reversibility.UNKNOWN),
		verification=VerificationSpec(
			source=VerificationSource.BROWSER,
			procedure='inspect application status',
			observation_url=observation_url,
			observation_is_read_only=observation_url is not None,
		),
	)
	return SemanticContract(contract_id='c1', task_id='task', version=1, units=(locate, fill, unit))


class CrashAfterAttemptStorage(SQLiteRuntimeStorage):
	"""Inject a process death after ATTEMPTED is durable."""

	crash_on_attempt = True

	def append_effect(self, draft: EffectRecordDraft):
		"""Persist the fact first, then interrupt before verification."""
		record = super().append_effect(draft)
		if draft.status is EffectRecordStatus.ATTEMPTED and self.crash_on_attempt:
			self.crash_on_attempt = False
			raise SimulatedCrash('after attempted')
		return record


class CrashAfterPreparedStorage(SQLiteRuntimeStorage):
	"""Interrupt after PREPARED and its checkpoint are both durable."""

	crash_on_prepared = True

	def commit_effect_and_checkpoint(self, draft, checkpoint):
		"""Persist PREPARED before preventing the native browser action."""
		committed = super().commit_effect_and_checkpoint(draft, checkpoint)
		if draft.status is EffectRecordStatus.PREPARED and self.crash_on_prepared:
			self.crash_on_prepared = False
			raise SimulatedCrash('after prepared')
		return committed


class CrashAfterFinalizeAgent(Agent):
	"""Stop after native history receives the first finalized Browser step."""

	async def _finalize(self, browser_state_summary):
		await super()._finalize(browser_state_summary)
		raise SimulatedCrash('after native history append')


@pytest.mark.asyncio
async def test_finalized_history_is_durable_before_step_end_hook(tmp_path: Path, httpserver) -> None:
	httpserver.expect_request('/').respond_with_data('<html><h1>Application #7</h1></html>', content_type='text/html')
	browser = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
	agent = CrashAfterFinalizeAgent(
		task='Locate application 7',
		llm=ScriptedLLM([navigate_output(httpserver.url_for('/'))]),
		browser_session=browser,
		use_vision=False,
		directly_open_url=False,
		enable_signal_handler=False,
		final_response_after_failure=False,
	)
	history_path = tmp_path / 'history.json'
	try:
		with SQLiteRuntimeStorage(tmp_path / 'runtime.db') as storage:
			harness = RecoverableHarness(
				agent=agent,
				contract=contract(),
				storage=storage,
				workflow_id='wf-finalize',
				history_path=history_path,
				verifier=BrowserObservationVerifier(BrowserObservationSource(), PageInterpreter()),
			)
			with pytest.raises(SimulatedCrash):
				await harness.run(max_steps=2)
			assert len(json.loads(history_path.read_text(encoding='utf-8'))['history']) == 1
	finally:
		await browser.kill()


@pytest.mark.parametrize('scenario', ['normal', 'after_prepared', 'after_attempted', 'inconclusive'])
@pytest.mark.asyncio
async def test_native_click_is_guarded_and_history_is_durable(tmp_path: Path, httpserver, scenario: str) -> None:
	world = {'count': 0}

	def page() -> str:
		status = 'SUBMITTED' if world['count'] else 'NOT_SUBMITTED'
		return f'<html><body><h1>Application #7</h1><p>Application #7 Status: {status}</p><form method="post" action="/submit"><input name="name" aria-label="Application #7 name" /><button type="submit">Submit Application</button></form></body></html>'

	httpserver.expect_request('/').respond_with_handler(lambda request: Response(page(), content_type='text/html'))

	def submit(request):
		world['count'] += 1
		return Response(page(), content_type='text/html')

	httpserver.expect_request('/submit', method='POST').respond_with_handler(submit)
	url = httpserver.url_for('/')
	browser = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
	llm = ClickScript(url)
	agent = Agent(
		task='Submit application 7 once',
		llm=llm,
		browser_session=browser,
		max_actions_per_step=3,
		use_vision=False,
		directly_open_url=False,
		enable_signal_handler=False,
		final_response_after_failure=False,
	)
	interpreter = PageInterpreter(allow_submit_evidence=scenario != 'inconclusive')
	verifier = BrowserObservationVerifier(BrowserObservationSource(), interpreter)
	history_path = tmp_path / 'history.json'
	try:
		storage_type = {
			'normal': SQLiteRuntimeStorage,
			'after_prepared': CrashAfterPreparedStorage,
			'after_attempted': CrashAfterAttemptStorage,
			'inconclusive': SQLiteRuntimeStorage,
		}[scenario]
		with storage_type(tmp_path / 'runtime.db') as storage:
			harness = RecoverableHarness(
				agent=agent,
				contract=contract(url),
				storage=storage,
				workflow_id='wf-1',
				history_path=history_path,
				verifier=verifier,
			)
			if scenario in {'after_prepared', 'after_attempted'}:
				with pytest.raises(SimulatedCrash):
					await harness.run(max_steps=6)
				assert [record.status.value for record in storage.read_effects('wf-1')] == (
					['prepared'] if scenario == 'after_prepared' else ['prepared', 'attempted']
				)
				assert len(json.loads(history_path.read_text(encoding='utf-8'))['history']) == 2
			elif scenario == 'inconclusive':
				with pytest.raises(RuntimeProgressBlockedError):
					await harness.run(max_steps=4)
				assert harness.adapter.states['u_submit'].status is UnitStatus.UNKNOWN
				assert [record.status.value for record in storage.read_effects('wf-1')] == ['prepared', 'attempted', 'unknown']
			if scenario != 'normal':
				resumed_browser = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
				if scenario == 'after_prepared':
					resume_llm = ClickScript(url)
					resume_llm._index = 2
				else:
					resume_llm = ScriptedLLM([done_output()])
				resumed_agent = Agent(
					task='Finish application 7 workflow',
					llm=resume_llm,
					browser_session=resumed_browser,
					use_vision=False,
					directly_open_url=False,
					enable_signal_handler=False,
					final_response_after_failure=False,
				)
				try:
					interpreter.allow_submit_evidence = True
					resumed = await RecoverableHarness.resume(
						agent=resumed_agent,
						storage=storage,
						workflow_id='wf-1',
						history_path=history_path,
						verifier=verifier,
					)
					if scenario == 'after_prepared':
						assert resumed.adapter.states['u_submit'].status is UnitStatus.ACTIVE
						assert world['count'] == 0
					else:
						assert all(state.status is UnitStatus.COMPLETED for state in resumed.adapter.states.values())
					await resumed.run(max_steps=6)
				finally:
					await resumed_browser.kill()
			else:
				await harness.run(max_steps=6)
			assert world['count'] == 1
			if scenario == 'normal':
				assert all(state.status is UnitStatus.COMPLETED for state in harness.adapter.states.values())
			assert [record.status.value for record in storage.read_effects('wf-1')] == {
				'normal': ['prepared', 'attempted', 'committed'],
				'after_prepared': ['prepared', 'not_applied', 'prepared', 'attempted', 'committed'],
				'after_attempted': ['prepared', 'attempted', 'committed'],
				'inconclusive': ['prepared', 'attempted', 'unknown', 'committed'],
			}[scenario]
			assert len(json.loads(history_path.read_text(encoding='utf-8'))['history']) >= (
				3 if scenario == 'after_attempted' else 4
			)
			assert 'u_locate' in llm.prompts[1] and 'Completed semantic units' in llm.prompts[1]
			assert 'Navigated to' not in llm.prompts[1]
			if scenario == 'normal':
				assert 'u_submit' in llm.prompts[3] and 'Completed semantic units' in llm.prompts[3]
				assert 'Clicked button' not in llm.prompts[3]
				persisted = json.loads(history_path.read_text(encoding='utf-8'))['history']
				assert persisted[0]['model_output']['completion_claim_unit_id'] == 'u_locate'
				assert persisted[2]['model_output']['effect_boundary_action_index'] == 0
	finally:
		await browser.kill()
