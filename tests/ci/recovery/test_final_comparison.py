"""Process-level final comparison of native history and RecoverableHarness."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import psutil
import pytest
import uvicorn

from experiments.final_comparison.models import Mode, Scenario, TrialResult
from experiments.recovery_smoke import server as smoke_server_module
from experiments.recovery_smoke.server import _render


def test_crashed_worker_descendants_are_reaped(tmp_path: Path) -> None:
	from experiments.final_comparison.runner import _run_worker_process

	command = [
		sys.executable,
		'-c',
		'import os, subprocess, sys, time; '
		'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
		'print(child.pid, flush=True); time.sleep(0.5); os._exit(91)',
	]
	child_pid: int | None = None
	try:
		result = _run_worker_process(command, cwd=tmp_path, timeout=10)
		child_pid = int(result.stdout.strip())
		assert result.returncode == 91
		try:
			assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
		except psutil.NoSuchProcess:
			pass
	finally:
		if child_pid is not None:
			try:
				child = psutil.Process(child_pid)
				if child.status() != psutil.STATUS_ZOMBIE:
					child.kill()
			except psutil.NoSuchProcess:
				pass


def test_controlled_application_has_name_field_for_native_input() -> None:
	page = _render('NOT_SUBMITTED', 0)
	assert '<input name="name"' in page
	assert 'id="submit-btn"' in page


def test_shared_action_script_marks_only_the_harness_click_boundary() -> None:
	from experiments.final_comparison.workflow import click_output, input_output, navigate_output

	navigate = navigate_output('http://127.0.0.1:8765')
	fill = input_output(12)
	native_click = click_output(15, harness=False)
	harness_click = click_output(15, harness=True)
	assert [next(iter(item['action'][0])) for item in (navigate, fill, native_click, harness_click)] == [
		'navigate',
		'input',
		'click',
		'click',
	]
	assert native_click['action'] == harness_click['action']
	assert 'effect_boundary_action_index' not in native_click
	assert harness_click['effect_boundary_action_index'] == 0


@pytest.fixture
async def comparison_server(tmp_path: Path):
	smoke_server_module._db_path = tmp_path / 'world.db'
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		port = sock.getsockname()[1]
	config = uvicorn.Config(smoke_server_module.app, host='127.0.0.1', port=port, log_level='warning')
	server = uvicorn.Server(config)
	task = asyncio.create_task(server.serve())
	url = f'http://127.0.0.1:{port}'
	async with httpx.AsyncClient() as client:
		for _ in range(100):
			try:
				response = await client.get(f'{url}/status')
				response.raise_for_status()
				break
			except httpx.HTTPError:
				await asyncio.sleep(0.1)
		else:
			raise RuntimeError('comparison server did not start')
	yield url
	server.should_exit = True
	await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_effect_without_attempted_is_reconciled_without_duplicate(tmp_path: Path, comparison_server: str) -> None:
	from experiments.final_comparison.runner import run_trial

	result = await asyncio.to_thread(
		run_trial,
		mode=Mode.HARNESS,
		scenario=Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED,
		trial=1,
		server_url=comparison_server,
		workdir=tmp_path,
	)
	assert result.submit_count_after_crash == 1
	assert result.history_actions_after_crash == ('navigate', 'input')
	assert result.ledger_after_crash == ('prepared',)
	assert result.final_submit_count == 1
	assert result.safe_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', list(Scenario))
@pytest.mark.parametrize('mode', list(Mode))
async def test_six_process_windows_use_native_actions_and_external_truth(
	tmp_path: Path, comparison_server: str, mode: Mode, scenario: Scenario
) -> None:
	from experiments.final_comparison.runner import run_trial

	result = await asyncio.to_thread(
		run_trial, mode=mode, scenario=scenario, trial=1, server_url=comparison_server, workdir=tmp_path
	)
	assert result.task_completed
	assert result.history_actions_after_crash[:2] == ('navigate', 'input')
	assert 'submit_application' not in result.history_actions_after_crash
	assert result.submit_count_after_crash == (0 if scenario is Scenario.BEFORE_EFFECT else 1)
	assert result.duplicate_effect is (result.final_submit_count > 1)
	assert result.safe_recovery is (result.final_submit_count == 1 and not result.unsafe_retry)
	history_file = tmp_path / f'{mode.value}-{scenario.value}-1' / 'agent_history.json'
	final_history = json.loads(history_file.read_text(encoding='utf-8'))
	final_actions = [
		next(iter(action))
		for step in final_history['history']
		for action in (step.get('model_output') or {}).get('action', [])
		if action
	]
	assert final_actions[-1] == 'done'
	if mode is Mode.HARNESS:
		assert result.final_submit_count == 1
		assert result.ledger_after_crash[0] == 'prepared'
		if scenario is Scenario.VERIFIER_UNAVAILABLE:
			assert result.blocked_during_outage
	else:
		assert result.ledger_after_crash == ()


def test_report_preserves_per_scenario_rates_and_controlled_case_caveat(tmp_path: Path) -> None:
	from experiments.final_comparison.report import summarize, write_reports

	base = dict(
		scenario=Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED,
		trial=1,
		history_actions_after_crash=('navigate', 'input'),
		submit_count_after_crash=1,
		final_status='SUBMITTED',
		task_completed=True,
		actions_before_crash=3,
		actions_after_resume=2,
		replayed_actions=2,
		reexecuted_actions=3,
		recovery_duration_ms=150.0,
	)
	native = TrialResult.model_validate(
		{
			**base,
			'mode': Mode.NATIVE,
			'ledger_after_crash': (),
			'final_submit_count': 2,
			'safe_recovery': False,
			'duplicate_effect': True,
			'unsafe_retry': True,
		}
	)
	harness = TrialResult.model_validate(
		{
			**base,
			'mode': Mode.HARNESS,
			'ledger_after_crash': ('prepared',),
			'final_submit_count': 1,
			'safe_recovery': True,
			'duplicate_effect': False,
			'unsafe_retry': False,
		}
	)
	summary = summarize([native, harness])
	per_scenario = summary.per_scenario
	assert per_scenario[Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED.value]['native'].safe_recovery_rate == 0
	assert per_scenario[Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED.value]['harness'].safe_recovery_rate == 100
	write_reports([native, harness], tmp_path)
	assert len(json.loads((tmp_path / 'raw_trials.json').read_text(encoding='utf-8'))) == 2
	assert 'equally weighted controlled cases' in (tmp_path / 'summary.md').read_text(encoding='utf-8')
