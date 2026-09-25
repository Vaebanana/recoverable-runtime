"""The identical native Browser Action sequence used by both benchmark arms."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from browser_use.agent.views import RerunSummaryAction
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.recovery.contracts import (
	ConditionSpec,
	EffectSpec,
	Idempotency,
	Reversibility,
	SemanticContract,
	SemanticUnit,
	TargetSpec,
	UnitIdentity,
	VerificationSource,
	VerificationSpec,
)
from browser_use.recovery.observational_verifier import ObservationEvidence, ObservationSnapshot
from experiments.native_history_baseline.scripted_llm import ScriptedLLM, done_output


def _output(action: dict[str, dict[str, Any]], goal: str, **metadata: object) -> dict[str, Any]:
	return {
		'thinking': 'deterministic recovery comparison',
		'evaluation_previous_goal': 'scripted state',
		'memory': '',
		'next_goal': goal,
		'action': [action],
		**metadata,
	}


def navigate_output(url: str) -> dict[str, Any]:
	"""Open the same controlled application in either mode."""
	return _output({'navigate': {'url': url}}, 'Locate application #7', completion_claim_unit_id='u_locate')


def input_output(index: int) -> dict[str, Any]:
	"""Fill the applicant name using the native Browser Use input action."""
	return _output({'input': {'index': index, 'text': 'John Doe'}}, 'Fill applicant name', completion_claim_unit_id='u_fill')


def click_output(index: int, *, harness: bool) -> dict[str, Any]:
	"""Click Submit natively; only the Harness arm declares its boundary."""
	return _output(
		{'click': {'index': index}},
		'Submit application #7',
		**({'effect_boundary_action_index': 0} if harness else {}),
	)


class WorkflowScript(ScriptedLLM):
	"""Choose the same navigate, input, click, done steps from the live DOM."""

	def __init__(self, url: str, *, harness: bool, start_step: int = 0, resume_prep: bool = False) -> None:
		super().__init__()
		self.url = url
		self.harness = harness
		self.step = start_step
		self.resume_prep = resume_prep

	async def ainvoke(self, *args: object, **kwargs: object) -> ChatInvokeCompletion:
		output_format = kwargs.get('output_format') or (args[1] if len(args) >= 2 else None)
		if output_format is RerunSummaryAction:
			return await super().ainvoke(*args, **kwargs)

		messages = str(args[0]) if args else ''
		if self.step == 0:
			payload = navigate_output(self.url)
		elif self.step == 1:
			match = re.search(r'\[(\d+)\]<input name=name', messages)
			if match is None:
				raise RuntimeError('applicant name input is absent from Browser Use DOM')
			payload = input_output(int(match.group(1)))
			if self.resume_prep:
				payload.pop('completion_claim_unit_id', None)
		elif self.step == 2:
			match = re.search(r'\[(\d+)\]<button type=submit', messages)
			if match is None:
				raise RuntimeError('submit button is absent from Browser Use DOM')
			payload = click_output(int(match.group(1)), harness=self.harness)
		else:
			payload = done_output()
		if not self.harness:
			payload.pop('completion_claim_unit_id', None)
		self.step += 1
		if not isinstance(output_format, type) or not issubclass(output_format, BaseModel):
			raise TypeError('Browser Use did not provide an AgentOutput model')
		return ChatInvokeCompletion(completion=output_format.model_validate(payload), usage=None)


def build_contract(url: str) -> SemanticContract:
	"""Describe the three semantic units around the shared native actions."""
	target = TargetSpec(type='application', key='application/7', attributes={'application_id': '7'})
	plain = EffectSpec(has_side_effect=False, idempotency=Idempotency.NOT_APPLICABLE, reversibility=Reversibility.NOT_APPLICABLE)
	locate = SemanticUnit(
		unit_id='u_locate',
		identity=UnitIdentity(intent_key='locate_application', target_key='application/7', outcome_key='located'),
		intent='locate application #7',
		target=target,
		postconditions=(ConditionSpec(description='application page is visible', expected_observation='Application #7'),),
		effect=plain,
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='inspect application page'),
	)
	fill = SemanticUnit(
		unit_id='u_fill',
		identity=UnitIdentity(intent_key='fill_applicant_name', target_key='application/7', outcome_key='filled'),
		intent='fill applicant name',
		target=target,
		depends_on=('u_locate',),
		postconditions=(ConditionSpec(description='applicant name is John Doe', expected_observation='John Doe'),),
		effect=plain,
		verification=VerificationSpec(source=VerificationSource.BROWSER, procedure='inspect application form'),
	)
	submit = SemanticUnit(
		unit_id='u_submit',
		identity=UnitIdentity(intent_key='submit_application', target_key='application/7', outcome_key='submitted'),
		intent='submit application #7',
		target=target,
		depends_on=('u_fill',),
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
			observation_url=url,
			observation_is_read_only=True,
		),
	)
	return SemanticContract(
		contract_id='final-application-7', task_id='submit-application-7', version=1, units=(locate, fill, submit)
	)


class PageInterpreter:
	"""Extract only quotes grounded in the observed application DOM."""

	def __init__(self, *, available: bool = True) -> None:
		self.available = available

	async def extract(self, unit: SemanticUnit, snapshot: ObservationSnapshot) -> ObservationEvidence:
		if not self.available:
			return ObservationEvidence(target_quote='', condition_quotes=('',))
		quote = {'u_locate': 'Application #7', 'u_fill': 'John Doe', 'u_submit': 'Status: SUBMITTED'}[unit.unit_id]
		positive = quote if quote in snapshot.text else ''
		negative = 'Status: NOT_SUBMITTED' if unit.unit_id == 'u_submit' and 'Status: NOT_SUBMITTED' in snapshot.text else None
		record = next(
			(line for line in snapshot.text.splitlines() if 'Application #7' in line and (positive or negative or '') in line), ''
		)
		return ObservationEvidence(
			record_quote=record,
			target_quote='Application #7' if 'Application #7' in snapshot.text else '',
			condition_quotes=(positive,),
			negative_quote=negative,
		)
