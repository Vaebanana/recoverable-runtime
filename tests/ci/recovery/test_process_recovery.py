"""Day 10: hard process death followed by SQLite-only recovery."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from browser_use.recovery import EffectStatus, UnitStatus, VerificationStatus
from browser_use.recovery.persistence import SQLiteRuntimeStorage
from browser_use.recovery.persistence.models import EffectRecordStatus
from experiments.process_recovery import run_experiment
from experiments.process_recovery.scenario import (
	EXIT_AFTER_ATTEMPTED,
	EXIT_AFTER_PREPARED,
	UNIT_ID,
	ProcessScenario,
)
from experiments.recovery_smoke import server as smoke_server_module

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_experiment_writes_observed_results_after_temporary_databases_close(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Keep crash and recovery evidence after the experiment's SQLite files are removed."""
	monkeypatch.setattr(run_experiment, 'RESULTS_DIR', tmp_path)
	monkeypatch.setattr(sys, 'argv', ['run_experiment'])

	run_experiment.main()

	report = json.loads((tmp_path / 'day10_results.json').read_text(encoding='utf-8'))
	markdown = (tmp_path / 'day10_summary.md').read_text(encoding='utf-8')
	assert report['experiment'] == 'day10_process_recovery'
	assert report['generated_at']
	assert report['all_passed'] is True
	assert len(report['results']) == 3
	by_scenario = {item['scenario']: item for item in report['results']}
	assert set(by_scenario) == {'after_prepared', 'after_attempted', 'verifier_unavailable'}
	for item in by_scenario.values():
		assert item['final_unit_status'] == 'completed'
		assert item['final_effect_status'] == 'committed'
		assert item['verification_status'] == 'verified'
		assert item['submit_count'] == 1
		assert item['passed'] is True

	prepared = by_scenario['after_prepared']
	assert prepared['crash_point'] == 'after_prepared'
	assert prepared['ledger_after_crash'] == ['prepared']
	assert prepared['checkpoint_last_effect_seq_after_crash'] == prepared['ledger_last_effect_seq_after_crash']
	assert by_scenario['after_attempted']['ledger_after_crash'] == ['prepared', 'attempted']
	assert (
		by_scenario['after_attempted']['checkpoint_last_effect_seq_after_crash']
		< by_scenario['after_attempted']['ledger_last_effect_seq_after_crash']
	)
	assert by_scenario['verifier_unavailable']['ledger_after_crash'] == ['prepared', 'attempted']
	assert by_scenario['verifier_unavailable']['ledger_after_first_recovery'] == ['prepared', 'attempted', 'unknown']
	assert by_scenario['verifier_unavailable']['status_after_first_recovery'] == 'unknown'
	assert '| after_attempted |' in markdown
	assert '| verifier_unavailable |' in markdown
	assert 'PASS' in markdown


@pytest.fixture
async def process_recovery_server(tmp_path: Path):
	"""Run one durable external world that is independent of every worker process."""
	smoke_server_module._db_path = tmp_path / 'world.db'
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		port = sock.getsockname()[1]

	config = uvicorn.Config(smoke_server_module.app, host='127.0.0.1', port=port, log_level='warning')
	server = uvicorn.Server(config)
	task = asyncio.create_task(server.serve())
	base_url = f'http://127.0.0.1:{port}'

	async with httpx.AsyncClient() as client:
		deadline = asyncio.get_running_loop().time() + 10
		while True:
			try:
				response = await client.get(f'{base_url}/status')
				response.raise_for_status()
				break
			except Exception:
				if asyncio.get_running_loop().time() >= deadline:
					server.should_exit = True
					task.cancel()
					raise
				await asyncio.sleep(0.1)

	yield base_url

	server.should_exit = True
	try:
		await asyncio.wait_for(task, timeout=10)
	except (asyncio.CancelledError, TimeoutError):
		pass


async def _reset_world(server_url: str) -> dict[str, object]:
	async with httpx.AsyncClient() as client:
		response = await client.post(f'{server_url}/reset')
		response.raise_for_status()
		return response.json()


async def _world(server_url: str) -> dict[str, object]:
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{server_url}/status')
		response.raise_for_status()
		return response.json()


async def _run_worker(
	*,
	phase: str,
	scenario: ProcessScenario,
	runtime_db: Path,
	server_url: str,
	workflow_id: str,
	run_id: str,
) -> int:
	process = await asyncio.create_subprocess_exec(
		sys.executable,
		'-m',
		'experiments.process_recovery.worker',
		'--phase',
		phase,
		'--scenario',
		scenario.value,
		'--runtime-db',
		str(runtime_db),
		'--server-url',
		server_url,
		'--workflow-id',
		workflow_id,
		'--run-id',
		run_id,
		cwd=str(REPO_ROOT),
	)
	return await process.wait()


def _assert_final_completed(runtime_db: Path, workflow_id: str) -> None:
	with SQLiteRuntimeStorage(runtime_db) as storage:
		checkpoint = storage.load_latest_checkpoint(workflow_id)
		assert checkpoint is not None
		state = checkpoint.unit_states[UNIT_ID]
		assert state.status is UnitStatus.COMPLETED
		assert state.effect_status is EffectStatus.COMMITTED
		assert state.verification_status is VerificationStatus.VERIFIED


@pytest.mark.asyncio
async def test_hard_crash_after_prepared_recovers_with_new_attempt(
	tmp_path: Path,
	process_recovery_server: str,
) -> None:
	await _reset_world(process_recovery_server)
	runtime_db = tmp_path / 'after_prepared.db'
	workflow_id = 'process-after-prepared'

	exit_code = await _run_worker(
		phase='crash',
		scenario=ProcessScenario.AFTER_PREPARED,
		runtime_db=runtime_db,
		server_url=process_recovery_server,
		workflow_id=workflow_id,
		run_id='run-1',
	)
	assert exit_code == EXIT_AFTER_PREPARED
	assert (await _world(process_recovery_server))['submit_count'] == 0

	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [EffectRecordStatus.PREPARED]
		checkpoint = storage.load_latest_checkpoint(workflow_id)
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == records[0].seq
		original_attempt = records[0].attempt_id
		original_effect = records[0].effect_id

	assert (
		await _run_worker(
			phase='recover',
			scenario=ProcessScenario.AFTER_PREPARED,
			runtime_db=runtime_db,
			server_url=process_recovery_server,
			workflow_id=workflow_id,
			run_id='run-2',
		)
		== 0
	)

	world = await _world(process_recovery_server)
	assert world['status'] == 'SUBMITTED'
	assert world['submit_count'] == 1

	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.NOT_APPLIED,
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.COMMITTED,
		]
		assert records[1].attempt_id == original_attempt
		assert records[1].effect_id == original_effect
		retry_attempts = {record.attempt_id for record in records[2:]}
		retry_effects = {record.effect_id for record in records[2:]}
		assert len(retry_attempts) == 1
		assert len(retry_effects) == 1
		assert original_attempt not in retry_attempts
		assert original_effect not in retry_effects

	_assert_final_completed(runtime_db, workflow_id)


@pytest.mark.asyncio
async def test_hard_crash_after_attempted_reuses_original_attempt(
	tmp_path: Path,
	process_recovery_server: str,
) -> None:
	await _reset_world(process_recovery_server)
	runtime_db = tmp_path / 'after_attempted.db'
	workflow_id = 'process-after-attempted'

	exit_code = await _run_worker(
		phase='crash',
		scenario=ProcessScenario.AFTER_ATTEMPTED,
		runtime_db=runtime_db,
		server_url=process_recovery_server,
		workflow_id=workflow_id,
		run_id='run-1',
	)
	assert exit_code == EXIT_AFTER_ATTEMPTED
	assert (await _world(process_recovery_server))['submit_count'] == 1

	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
		]
		checkpoint = storage.load_latest_checkpoint(workflow_id)
		assert checkpoint is not None
		assert checkpoint.last_effect_seq == records[0].seq
		assert records[1].seq > checkpoint.last_effect_seq
		original_attempt = records[0].attempt_id
		original_effect = records[0].effect_id
		assert {record.attempt_id for record in records} == {original_attempt}
		assert {record.effect_id for record in records} == {original_effect}

	assert (
		await _run_worker(
			phase='recover',
			scenario=ProcessScenario.AFTER_ATTEMPTED,
			runtime_db=runtime_db,
			server_url=process_recovery_server,
			workflow_id=workflow_id,
			run_id='run-2',
		)
		== 0
	)

	assert (await _world(process_recovery_server))['submit_count'] == 1
	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.COMMITTED,
		]
		assert {record.attempt_id for record in records} == {original_attempt}
		assert {record.effect_id for record in records} == {original_effect}

	_assert_final_completed(runtime_db, workflow_id)


@pytest.mark.asyncio
async def test_unknown_survives_multiple_process_restarts(
	tmp_path: Path,
	process_recovery_server: str,
) -> None:
	await _reset_world(process_recovery_server)
	runtime_db = tmp_path / 'verifier_unavailable.db'
	workflow_id = 'process-verifier-unavailable'

	exit_code = await _run_worker(
		phase='crash',
		scenario=ProcessScenario.VERIFIER_UNAVAILABLE,
		runtime_db=runtime_db,
		server_url=process_recovery_server,
		workflow_id=workflow_id,
		run_id='run-1',
	)
	assert exit_code == EXIT_AFTER_ATTEMPTED
	assert (await _world(process_recovery_server))['submit_count'] == 1

	assert (
		await _run_worker(
			phase='recover-inconclusive',
			scenario=ProcessScenario.VERIFIER_UNAVAILABLE,
			runtime_db=runtime_db,
			server_url=process_recovery_server,
			workflow_id=workflow_id,
			run_id='run-2',
		)
		== 0
	)

	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.UNKNOWN,
		]
		assert len({record.attempt_id for record in records}) == 1
		assert len({record.effect_id for record in records}) == 1
		checkpoint = storage.load_latest_checkpoint(workflow_id)
		assert checkpoint is not None
		state = checkpoint.unit_states[UNIT_ID]
		assert state.status is UnitStatus.UNKNOWN
		assert state.effect_status is EffectStatus.UNKNOWN
		assert state.verification_status is VerificationStatus.INCONCLUSIVE

	assert (await _world(process_recovery_server))['submit_count'] == 1

	assert (
		await _run_worker(
			phase='recover',
			scenario=ProcessScenario.VERIFIER_UNAVAILABLE,
			runtime_db=runtime_db,
			server_url=process_recovery_server,
			workflow_id=workflow_id,
			run_id='run-3',
		)
		== 0
	)

	assert (await _world(process_recovery_server))['submit_count'] == 1
	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		assert [record.status for record in records] == [
			EffectRecordStatus.PREPARED,
			EffectRecordStatus.ATTEMPTED,
			EffectRecordStatus.UNKNOWN,
			EffectRecordStatus.COMMITTED,
		]
		assert len({record.attempt_id for record in records}) == 1
		assert len({record.effect_id for record in records}) == 1

	_assert_final_completed(runtime_db, workflow_id)
