"""Run Native Browser Use History vs Recoverable Harness and write fixed reports."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from browser_use.recovery.persistence import SQLiteRuntimeStorage
from experiments.native_history_baseline.models import CrashPoint, RecoveryMode, TrialResult
from experiments.native_history_baseline.worker import CRASH_EXIT_CODE

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).resolve().parent / 'results'
JSON_REPORT = RESULTS_DIR / 'native_history_results.json'
MARKDOWN_REPORT = RESULTS_DIR / 'native_history_summary.md'


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


def _reset_world(server_url: str) -> None:
	response = httpx.post(f'{server_url}/reset', timeout=5.0)
	response.raise_for_status()


def _world(server_url: str) -> dict[str, object]:
	response = httpx.get(f'{server_url}/status', timeout=5.0)
	response.raise_for_status()
	return response.json()


def _raw_history_actions(history_path: Path) -> tuple[str, ...]:
	data = json.loads(history_path.read_text(encoding='utf-8'))
	names: list[str] = []
	for item in data.get('history', []):
		model_output = item.get('model_output') or {}
		for action in model_output.get('action', []):
			if action:
				names.append(next(iter(action)))
	return tuple(names)


def _worker(
	*,
	phase: str,
	mode: RecoveryMode,
	crash_point: CrashPoint,
	server_url: str,
	history_file: Path,
	runtime_db: Path,
	workflow_id: str,
	run_id: str,
) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[
			sys.executable,
			'-m',
			'experiments.native_history_baseline.worker',
			'--phase',
			phase,
			'--mode',
			mode.value,
			'--crash-point',
			crash_point.value,
			'--server-url',
			server_url,
			'--history-file',
			str(history_file),
			'--runtime-db',
			str(runtime_db),
			'--workflow-id',
			workflow_id,
			'--run-id',
			run_id,
		],
		cwd=REPO_ROOT,
		text=True,
		check=False,
	)


def _expected_history(crash_point: CrashPoint) -> tuple[str, ...]:
	if crash_point is CrashPoint.AFTER_HISTORY_COMMIT:
		return ('navigate', 'submit_application')
	return ('navigate',)


def run_trial(
	*,
	mode: RecoveryMode,
	crash_point: CrashPoint,
	trial: int,
	server_url: str,
	workdir: Path,
) -> TrialResult:
	_reset_world(server_url)

	trial_dir = workdir / f'{mode.value}-{crash_point.value}-{trial}'
	trial_dir.mkdir(parents=True, exist_ok=True)
	history_file = trial_dir / 'agent_history.json'
	runtime_db = trial_dir / 'runtime.db'
	workflow_id = f'native-history-{mode.value}-{crash_point.value}-{trial}'

	crash = _worker(
		phase='crash',
		mode=mode,
		crash_point=crash_point,
		server_url=server_url,
		history_file=history_file,
		runtime_db=runtime_db,
		workflow_id=workflow_id,
		run_id='run-1',
	)
	if crash.returncode != CRASH_EXIT_CODE:
		raise RuntimeError(f'{mode.value}/{crash_point.value} crash worker exit={crash.returncode}, expected={CRASH_EXIT_CODE}')

	history_actions = _raw_history_actions(history_file)
	if history_actions != _expected_history(crash_point):
		raise AssertionError(f'unexpected durable history at {crash_point.value}: {history_actions}')

	world_after_crash = _world(server_url)
	submit_after_crash = int(str(world_after_crash['submit_count']))

	ledger_after_crash: tuple[str, ...] = ()
	if mode is RecoveryMode.HARNESS:
		with SQLiteRuntimeStorage(runtime_db) as storage:
			ledger_after_crash = tuple(record.status.value for record in storage.read_effects(workflow_id))

	recover = _worker(
		phase='recover',
		mode=mode,
		crash_point=crash_point,
		server_url=server_url,
		history_file=history_file,
		runtime_db=runtime_db,
		workflow_id=workflow_id,
		run_id='run-2',
	)
	if recover.returncode != 0:
		raise RuntimeError(f'{mode.value}/{crash_point.value} recovery worker exit={recover.returncode}')

	final_world = _world(server_url)
	final_submit_count = int(str(final_world['submit_count']))
	final_status = str(final_world['status'])
	task_completed = final_status == 'SUBMITTED'
	duplicate_effect = final_submit_count > 1
	unsafe_retry = (
		submit_after_crash > 0 and final_submit_count > submit_after_crash and 'submit_application' not in history_actions
	)
	safe_recovery = task_completed and not duplicate_effect and not unsafe_retry

	notes: list[str] = []
	if unsafe_retry:
		notes.append('Native history lacked the already-applied submit step, so resume submitted again.')
	if mode is RecoveryMode.HARNESS and crash_point is CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT:
		notes.append('AgentHistory lacked submit, but PREPARED/ATTEMPTED preserved effect uncertainty.')

	return TrialResult(
		mode=mode,
		crash_point=crash_point,
		trial=trial,
		history_actions_after_crash=history_actions,
		submit_count_after_crash=submit_after_crash,
		final_submit_count=final_submit_count,
		final_status=final_status,
		task_completed=task_completed,
		duplicate_effect=duplicate_effect,
		unsafe_retry=unsafe_retry,
		safe_recovery=safe_recovery,
		ledger_after_crash=ledger_after_crash,
		notes=tuple(notes),
	)


def _rate(results: list[TrialResult], field: str) -> float:
	if not results:
		return 0.0
	return 100.0 * sum(bool(getattr(item, field)) for item in results) / len(results)


def _write_reports(results: list[TrialResult]) -> None:
	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	generated_at = datetime.now(timezone.utc).isoformat()

	payload: dict[str, object] = {
		'experiment': 'native_browser_use_history_vs_harness',
		'generated_at': generated_at,
		'results': [result.model_dump(mode='json') for result in results],
		'aggregate': {},
	}
	aggregate = payload['aggregate']
	assert isinstance(aggregate, dict)

	for mode in RecoveryMode:
		group = [result for result in results if result.mode is mode]
		aggregate[mode.value] = {
			'trials': len(group),
			'task_completion_rate': _rate(group, 'task_completed'),
			'safe_recovery_rate': _rate(group, 'safe_recovery'),
			'duplicate_effect_rate': _rate(group, 'duplicate_effect'),
			'unsafe_retry_rate': _rate(group, 'unsafe_retry'),
		}

	JSON_REPORT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')

	lines = [
		'# Native Browser Use History vs Recoverable Harness',
		'',
		f'- Generated at: `{generated_at}`',
		'- Crash scenarios are equally weighted controlled cases, not production failure probabilities.',
		'',
		'| Mode | Crash point | History after crash | Ledger after crash | Crash submits | Final submits | Safe |',
		'| --- | --- | --- | --- | ---: | ---: | --- |',
	]
	for result in results:
		history = ' → '.join(result.history_actions_after_crash) or 'none'
		ledger = ' → '.join(result.ledger_after_crash) or 'n/a'
		lines.append(
			f'| {result.mode.value} | {result.crash_point.value} | {history} | {ledger} | '
			f'{result.submit_count_after_crash} | {result.final_submit_count} | '
			f'{"PASS" if result.safe_recovery else "FAIL"} |'
		)

	lines.extend(['', '## Aggregate', ''])
	for mode in RecoveryMode:
		stats = aggregate[mode.value]
		assert isinstance(stats, dict)
		lines.extend(
			[
				f'### {mode.value}',
				'',
				f'- Task Completion Rate: **{float(stats["task_completion_rate"]):.1f}%**',
				f'- Safe Recovery Rate: **{float(stats["safe_recovery_rate"]):.1f}%**',
				f'- Duplicate Effect Rate: **{float(stats["duplicate_effect_rate"]):.1f}%**',
				f'- Unsafe Retry Rate: **{float(stats["unsafe_retry_rate"]):.1f}%**',
				'',
			]
		)

	MARKDOWN_REPORT.write_text('\n'.join(lines), encoding='utf-8')


def main() -> None:
	parser = argparse.ArgumentParser(description='Run Native Browser Use history baseline experiment.')
	parser.add_argument('--repeats', type=int, default=5)
	args = parser.parse_args()
	if args.repeats < 1:
		raise ValueError('--repeats must be >= 1')

	with tempfile.TemporaryDirectory(prefix='native-history-baseline-') as temp_dir:
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
			results: list[TrialResult] = []
			for crash_point in CrashPoint:
				for mode in RecoveryMode:
					for trial in range(1, args.repeats + 1):
						result = run_trial(
							mode=mode,
							crash_point=crash_point,
							trial=trial,
							server_url=server_url,
							workdir=workdir,
						)
						results.append(result)
						print(
							f'{mode.value:7} {crash_point.value:46} '
							f'crash={result.submit_count_after_crash} '
							f'final={result.final_submit_count} safe={result.safe_recovery}'
						)

			_write_reports(results)
			counts = Counter((item.mode.value, item.safe_recovery) for item in results)
			print(f'\nWrote {JSON_REPORT}')
			print(f'Wrote {MARKDOWN_REPORT}')
			print(f'Safe recovery counts: {dict(counts)}')
		finally:
			server.terminate()
			try:
				server.wait(timeout=10)
			except subprocess.TimeoutExpired:
				server.kill()
				server.wait(timeout=10)


if __name__ == '__main__':
	main()
