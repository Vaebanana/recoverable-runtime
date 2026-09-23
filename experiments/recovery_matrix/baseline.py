"""Restart-from-task baseline with no durable effect history or reconciliation."""

from __future__ import annotations

from browser_use.browser.session import BrowserSession
from experiments.recovery_matrix.models import ExecutionMode, FaultScenario, TrialResult
from experiments.recovery_matrix.world import fetch_world_state, reset_world, submit_once


async def run_baseline_trial(
	*,
	scenario: FaultScenario,
	trial: int,
	server_url: str,
	browser_session: BrowserSession,
) -> TrialResult:
	"""Run a minimal restart-from-task baseline against the same real browser page.

	The baseline intentionally has no Semantic Runtime, Effect Ledger, Safety Gate,
	or Reconciliation. After a crash, an unconfirmed business task is restarted.
	"""
	await reset_world(server_url)
	retry_count = 0
	unsafe_retry_count = 0
	notes: list[str] = []

	if scenario is FaultScenario.AFTER_PREPARED:
		# Equivalent baseline crash window: the process dies before the external
		# effect is invoked. Restarting the task produces the first real submit.
		notes.append('crash occurred before the first external submit')
		retry_count = 1
		await submit_once(browser_session, server_url, expected_count=1)
	else:
		# The external effect happened, but the process has no durable knowledge
		# that can distinguish success from an unconfirmed attempt.
		await submit_once(browser_session, server_url, expected_count=1)
		notes.append('first submit reached external world before crash')
		if scenario is FaultScenario.VERIFIER_UNAVAILABLE:
			notes.append('completion evidence is unavailable after restart')

		# restart-from-task: execute the still-unconfirmed business effect again
		retry_count = 1
		unsafe_retry_count = 1
		await submit_once(browser_session, server_url, expected_count=2)

	state = await fetch_world_state(server_url)
	final_status = str(state.get('status', 'UNKNOWN'))
	submit_count = int(str(state.get('submit_count', 0)))
	task_completed = final_status == 'SUBMITTED'
	duplicate_effect = submit_count > 1
	safe_recovery = task_completed and submit_count == 1 and unsafe_retry_count == 0

	return TrialResult(
		mode=ExecutionMode.BASELINE,
		scenario=scenario,
		trial=trial,
		final_status=final_status,
		submit_count=submit_count,
		retry_count=retry_count,
		unsafe_retry_count=unsafe_retry_count,
		duplicate_effect=duplicate_effect,
		task_completed=task_completed,
		safe_recovery=safe_recovery,
		notes=tuple(notes),
	)
