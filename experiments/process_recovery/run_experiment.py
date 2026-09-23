"""Standalone Day 10 process-level crash/recovery demonstration."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from browser_use.recovery.persistence import SQLiteRuntimeStorage
from experiments.process_recovery.scenario import (
	EXIT_AFTER_ATTEMPTED,
	EXIT_AFTER_PREPARED,
	UNIT_ID,
	ProcessScenario,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).resolve().parent / 'results'


class ScenarioResult(BaseModel):
	"""Observed crash-window and final recovery facts for one process scenario."""

	model_config = ConfigDict(frozen=True)

	scenario: str
	crash_point: str
	ledger_after_crash: list[str]
	ledger_last_effect_seq_after_crash: int
	checkpoint_last_effect_seq_after_crash: int
	final_unit_status: str
	final_effect_status: str
	verification_status: str
	submit_count: int
	passed: bool
	ledger_after_first_recovery: list[str] | None = None
	status_after_first_recovery: str | None = None


class ExperimentReport(BaseModel):
	"""Portable Day 10 conclusions retained after temporary SQLite cleanup."""

	model_config = ConfigDict(frozen=True)

	experiment: str
	generated_at: datetime
	results: list[ScenarioResult]
	all_passed: bool


def write_json_report(report: ExperimentReport) -> Path:
	"""Persist the structured experiment conclusions at a fixed path."""
	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	path = RESULTS_DIR / 'day10_results.json'
	path.write_text(json.dumps(report.model_dump(mode='json'), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
	return path


def write_markdown_report(report: ExperimentReport) -> Path:
	"""Persist a compact, human-readable summary of the observed outcomes."""
	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	path = RESULTS_DIR / 'day10_summary.md'
	lines = [
		'# Day 10 Process Recovery Experiment',
		'',
		f'- Generated at: `{report.generated_at.isoformat()}`',
		f'- Overall: `{"PASS" if report.all_passed else "FAIL"}`',
		'',
		'| Scenario | Crash ledger | Checkpoint seq | Ledger seq | Unit | Effect | Verification | Submit count | Result |',
		'| --- | --- | ---: | ---: | --- | --- | --- | ---: | --- |',
	]
	for item in report.results:
		lines.append(
			f'| {item.scenario} | {" → ".join(item.ledger_after_crash)} '
			f'| {item.checkpoint_last_effect_seq_after_crash} '
			f'| {item.ledger_last_effect_seq_after_crash} '
			f'| {item.final_unit_status} | {item.final_effect_status} '
			f'| {item.verification_status} | {item.submit_count} '
			f'| {"PASS" if item.passed else "FAIL"} |'
		)
	path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
	return path


def _free_port() -> int:
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return int(sock.getsockname()[1])


def _wait_for_server(server_url: str, timeout: float = 10.0) -> None:
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		try:
			response = httpx.get(f'{server_url}/status', timeout=1.0)
			response.raise_for_status()
			return
		except Exception:
			time.sleep(0.1)
	raise RuntimeError(f'application server did not become ready: {server_url}')


def _run_worker(
	*,
	phase: str,
	scenario: ProcessScenario,
	runtime_db: Path,
	server_url: str,
	workflow_id: str,
	run_id: str,
) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[
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
		],
		cwd=REPO_ROOT,
		text=True,
		check=False,
	)


def _print_result(*, scenario: ProcessScenario, runtime_db: Path, server_url: str) -> None:
	workflow_id = f'day10-{scenario.value}'
	with SQLiteRuntimeStorage(runtime_db) as storage:
		records = storage.read_effects(workflow_id)
		checkpoint = storage.load_latest_checkpoint(workflow_id)
	world = httpx.get(f'{server_url}/status', timeout=5.0).json()

	print(f'\nScenario: {scenario.value}')
	print('Ledger:', ' -> '.join(record.status.value for record in records))
	print('Attempt IDs:', [record.attempt_id for record in records])
	print('Effect IDs:', [record.effect_id for record in records])
	if checkpoint is not None:
		state = checkpoint.unit_states[UNIT_ID]
		print(
			'Final Runtime:',
			f'{state.status.value}/{state.effect_status.value}/{state.verification_status.value}',
		)
	print('External world:', world)


def run_scenario(scenario: ProcessScenario, workdir: Path, server_url: str) -> ScenarioResult:
	"""Run one crash/recovery scenario and return facts read from its SQLite stores."""
	runtime_db = workdir / f'{scenario.value}.db'
	workflow_id = f'day10-{scenario.value}'

	httpx.post(f'{server_url}/reset', timeout=5.0).raise_for_status()
	crash = _run_worker(
		phase='crash',
		scenario=scenario,
		runtime_db=runtime_db,
		server_url=server_url,
		workflow_id=workflow_id,
		run_id='run-1',
	)
	expected_exit = EXIT_AFTER_PREPARED if scenario is ProcessScenario.AFTER_PREPARED else EXIT_AFTER_ATTEMPTED
	if crash.returncode != expected_exit:
		raise RuntimeError(f'crash worker exit={crash.returncode}, expected={expected_exit}')
	with SQLiteRuntimeStorage(runtime_db) as storage:
		crash_records = storage.read_effects(workflow_id)
		crash_checkpoint = storage.load_latest_checkpoint(workflow_id)
	if not crash_records or crash_checkpoint is None:
		raise RuntimeError(f'crash evidence missing for {scenario.value}')
	ledger_after_crash = [record.status.value for record in crash_records]
	ledger_last_seq = crash_records[-1].seq
	checkpoint_last_seq = crash_checkpoint.last_effect_seq
	first_recovery_ledger: list[str] | None = None
	first_recovery_status: str | None = None

	if scenario is ProcessScenario.VERIFIER_UNAVAILABLE:
		first = _run_worker(
			phase='recover-inconclusive',
			scenario=scenario,
			runtime_db=runtime_db,
			server_url=server_url,
			workflow_id=workflow_id,
			run_id='run-2',
		)
		if first.returncode != 0:
			raise RuntimeError(f'first recovery worker failed with exit={first.returncode}')
		with SQLiteRuntimeStorage(runtime_db) as storage:
			first_records = storage.read_effects(workflow_id)
			first_checkpoint = storage.load_latest_checkpoint(workflow_id)
		if first_checkpoint is None:
			raise RuntimeError('first recovery checkpoint missing')
		first_recovery_ledger = [record.status.value for record in first_records]
		first_recovery_status = first_checkpoint.unit_states[UNIT_ID].status.value
		final_run_id = 'run-3'
	else:
		final_run_id = 'run-2'

	recover = _run_worker(
		phase='recover',
		scenario=scenario,
		runtime_db=runtime_db,
		server_url=server_url,
		workflow_id=workflow_id,
		run_id=final_run_id,
	)
	if recover.returncode != 0:
		raise RuntimeError(f'recovery worker failed with exit={recover.returncode}')

	with SQLiteRuntimeStorage(runtime_db) as storage:
		final_records = storage.read_effects(workflow_id)
		final_checkpoint = storage.load_latest_checkpoint(workflow_id)
	if not final_records or final_checkpoint is None:
		raise RuntimeError(f'final recovery evidence missing for {scenario.value}')
	final_state = final_checkpoint.unit_states[UNIT_ID]
	world = httpx.get(f'{server_url}/status', timeout=5.0).json()
	submit_count = int(world['submit_count'])
	crash_point = 'after_prepared' if crash.returncode == EXIT_AFTER_PREPARED else 'after_attempted'
	expected_crash_ledger = ['prepared'] if scenario is ProcessScenario.AFTER_PREPARED else ['prepared', 'attempted']
	crash_window_valid = (
		ledger_after_crash == expected_crash_ledger
		and checkpoint_last_seq == crash_records[0].seq
		and (
			ledger_last_seq == checkpoint_last_seq
			if scenario is ProcessScenario.AFTER_PREPARED
			else ledger_last_seq > checkpoint_last_seq
		)
	)
	first_recovery_valid = (
		first_recovery_ledger == ['prepared', 'attempted', 'unknown'] and first_recovery_status == 'unknown'
		if scenario is ProcessScenario.VERIFIER_UNAVAILABLE
		else True
	)
	final_ledger_valid = [record.status.value for record in final_records] == (
		['prepared', 'not_applied', 'prepared', 'attempted', 'committed']
		if scenario is ProcessScenario.AFTER_PREPARED
		else ['prepared', 'attempted', 'unknown', 'committed']
		if scenario is ProcessScenario.VERIFIER_UNAVAILABLE
		else ['prepared', 'attempted', 'committed']
	)
	result = ScenarioResult(
		scenario=scenario.value,
		crash_point=crash_point,
		ledger_after_crash=ledger_after_crash,
		ledger_last_effect_seq_after_crash=ledger_last_seq,
		checkpoint_last_effect_seq_after_crash=checkpoint_last_seq,
		final_unit_status=final_state.status.value,
		final_effect_status=final_state.effect_status.value,
		verification_status=final_state.verification_status.value,
		submit_count=submit_count,
		passed=(
			crash_window_valid
			and first_recovery_valid
			and final_ledger_valid
			and final_state.status.value == 'completed'
			and final_state.effect_status.value == 'committed'
			and final_state.verification_status.value == 'verified'
			and world['status'] == 'SUBMITTED'
			and submit_count == 1
		),
		ledger_after_first_recovery=first_recovery_ledger,
		status_after_first_recovery=first_recovery_status,
	)
	_print_result(scenario=scenario, runtime_db=runtime_db, server_url=server_url)
	return result


def main() -> None:
	parser = argparse.ArgumentParser(description='Run Day 10 hard-crash recovery scenarios.')
	parser.add_argument(
		'--scenario',
		choices=('all', *(scenario.value for scenario in ProcessScenario)),
		default='all',
	)
	args = parser.parse_args()

	with tempfile.TemporaryDirectory(prefix='recoverable-runtime-day10-') as temp_dir:
		workdir = Path(temp_dir)
		port = _free_port()
		server_url = f'http://127.0.0.1:{port}'
		server = subprocess.Popen(
			[
				sys.executable,
				'-m',
				'experiments.recovery_smoke.server',
				'--host',
				'127.0.0.1',
				'--port',
				str(port),
				'--db',
				str(workdir / 'world.db'),
			],
			cwd=REPO_ROOT,
		)
		try:
			_wait_for_server(server_url)
			scenarios = list(ProcessScenario) if args.scenario == 'all' else [ProcessScenario(args.scenario)]
			results = [run_scenario(scenario, workdir, server_url) for scenario in scenarios]
		finally:
			server.terminate()
			try:
				server.wait(timeout=10)
			except subprocess.TimeoutExpired:
				server.kill()
				server.wait(timeout=10)

	report = ExperimentReport(
		experiment='day10_process_recovery',
		generated_at=datetime.now(timezone.utc),
		results=results,
		all_passed=all(item.passed for item in results),
	)
	print(f'JSON report: {write_json_report(report)}')
	print(f'Markdown report: {write_markdown_report(report)}')


if __name__ == '__main__':
	main()
