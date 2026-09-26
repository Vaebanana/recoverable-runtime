"""Parent process for independent, externally scored recovery comparison trials."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx
import psutil

from browser_use.recovery.persistence.storage import SQLiteRuntimeStorage
from experiments.final_comparison.models import ActionTrace, Mode, Scenario, TrialResult
from experiments.final_comparison.worker import CRASH_EXIT_CODE

REPO_ROOT = Path(__file__).resolve().parents[2]


def _world(url: str) -> dict[str, object]:
	response = httpx.get(f'{url}/status', timeout=10)
	response.raise_for_status()
	return response.json()


def _reset(url: str) -> None:
	response = httpx.post(f'{url}/reset', timeout=10)
	response.raise_for_status()


def _history_actions(path: Path) -> tuple[str, ...]:
	data = json.loads(path.read_text(encoding='utf-8'))
	return tuple(
		next(iter(action)) for item in data['history'] for action in (item.get('model_output') or {}).get('action', []) if action
	)


def _trace(path: Path) -> tuple[ActionTrace, ...]:
	if not path.exists():
		return ()
	return tuple(ActionTrace.model_validate_json(line) for line in path.read_text(encoding='utf-8').splitlines() if line)


def _run_worker_process(command: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
	"""Capture and reap children of a worker that may terminate via os._exit."""
	process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	root = psutil.Process(process.pid)
	observed_children: dict[int, float] = {}
	deadline = time.monotonic() + timeout
	try:
		while True:
			try:
				for child in root.children(recursive=True):
					observed_children[child.pid] = child.create_time()
			except (psutil.NoSuchProcess, psutil.AccessDenied):
				pass
			try:
				process.communicate(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
				break
			except subprocess.TimeoutExpired:
				if process.poll() is not None:
					break
				if time.monotonic() >= deadline:
					raise
	finally:
		if process.poll() is None:
			process.kill()
		children: list[psutil.Process] = []
		for pid, created_at in observed_children.items():
			try:
				child = psutil.Process(pid)
				if child.create_time() == created_at:
					child.terminate()
					children.append(child)
			except (psutil.NoSuchProcess, psutil.AccessDenied):
				pass
		_, alive = psutil.wait_procs(children, timeout=2)
		for child in alive:
			try:
				if child.create_time() == observed_children[child.pid]:
					child.kill()
			except (psutil.NoSuchProcess, psutil.AccessDenied):
				pass
	stdout, stderr = process.communicate(timeout=5)
	return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _worker(
	*,
	phase: str,
	mode: Mode,
	scenario: Scenario,
	server_url: str,
	history_file: Path,
	trace_file: Path,
	runtime_db: Path,
	workflow_id: str,
) -> subprocess.CompletedProcess[str]:
	return _run_worker_process(
		[
			sys.executable,
			'-m',
			'experiments.final_comparison.worker',
			'--phase',
			phase,
			'--mode',
			mode.value,
			'--scenario',
			scenario.value,
			'--server-url',
			server_url,
			'--history-file',
			str(history_file),
			'--trace-file',
			str(trace_file),
			'--runtime-db',
			str(runtime_db),
			'--workflow-id',
			workflow_id,
		],
		cwd=REPO_ROOT,
		timeout=180,
	)


def _require_exit(result: subprocess.CompletedProcess[str], expected: int, label: str) -> None:
	if result.returncode != expected:
		raise RuntimeError(
			f'{label} worker exit={result.returncode}, expected={expected}\n'
			f'stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-6000:]}'
		)


def _expected_history(scenario: Scenario) -> tuple[str, ...]:
	if scenario is Scenario.NORMAL:
		return ('navigate', 'input', 'click', 'done')
	if scenario is Scenario.AFTER_HISTORY_COMMIT:
		return ('navigate', 'input', 'click')
	return ('navigate', 'input')


def _expected_ledger(scenario: Scenario) -> tuple[str, ...]:
	if scenario in {Scenario.BEFORE_EFFECT, Scenario.AFTER_EFFECT_BEFORE_ATTEMPTED}:
		return ('prepared',)
	if scenario in {Scenario.AFTER_ATTEMPTED_BEFORE_HISTORY, Scenario.VERIFIER_UNAVAILABLE}:
		return ('prepared', 'attempted')
	return ('prepared', 'attempted', 'committed')


def run_trial(*, mode: Mode, scenario: Scenario, trial: int, server_url: str, workdir: Path) -> TrialResult:
	"""Crash one worker, inspect independent state, then recover in new workers."""
	_reset(server_url)
	trial_dir = workdir / f'{mode.value}-{scenario.value}-{trial}'
	trial_dir.mkdir(parents=True, exist_ok=True)
	history_file = trial_dir / 'agent_history.json'
	trace_file = trial_dir / 'action_trace.jsonl'
	runtime_db = trial_dir / 'runtime.db'
	workflow_id = f'final-{mode.value}-{scenario.value}-{trial}'

	def run_phase(phase: str) -> subprocess.CompletedProcess[str]:
		return _worker(
			phase=phase,
			mode=mode,
			scenario=scenario,
			server_url=server_url,
			history_file=history_file,
			trace_file=trace_file,
			runtime_db=runtime_db,
			workflow_id=workflow_id,
		)

	initial = run_phase('initial')
	_require_exit(initial, 0 if scenario is Scenario.NORMAL else CRASH_EXIT_CODE, 'initial')
	history_actions = _history_actions(history_file)
	if history_actions != _expected_history(scenario):
		raise AssertionError(f'{mode.value}/{scenario.value}: unexpected durable history {history_actions}')
	crash_world = _world(server_url)
	crash_count = int(str(crash_world['submit_count']))
	expected_count = 0 if scenario is Scenario.BEFORE_EFFECT else 1
	if crash_count != expected_count:
		raise AssertionError(f'{mode.value}/{scenario.value}: crash world count={crash_count}, expected={expected_count}')
	ledger: tuple[str, ...] = ()
	if mode is Mode.HARNESS:
		with SQLiteRuntimeStorage(runtime_db) as storage:
			ledger = tuple(record.status.value for record in storage.read_effects(workflow_id))
		if ledger != _expected_ledger(scenario):
			raise AssertionError(f'{mode.value}/{scenario.value}: ledger={ledger}, expected={_expected_ledger(scenario)}')

	blocked = False
	started = time.monotonic()
	if scenario is Scenario.VERIFIER_UNAVAILABLE and mode is Mode.HARNESS:
		outage = run_phase('observe_unavailable')
		_require_exit(outage, 0, 'observation outage')
		blocked = any(item.phase == 'outage' and item.action == 'blocked_by_runtime_gate' for item in _trace(trace_file))
		if not blocked or int(str(_world(server_url)['submit_count'])) != crash_count:
			raise AssertionError('Harness retried an uncertain effect during observation outage')
	if scenario is not Scenario.NORMAL:
		recovered = run_phase('recover')
		_require_exit(recovered, 0, 'recovery')
	recovery_duration_ms = 1000 * (time.monotonic() - started) if scenario is not Scenario.NORMAL else 0.0
	final = _world(server_url)
	final_count = int(str(final['submit_count']))
	status = str(final['status'])
	completed = status == 'SUBMITTED'
	duplicate = final_count > 1
	unsafe_retry = crash_count > 0 and final_count > crash_count
	trace = _trace(trace_file)
	before = [item.action for item in trace if item.phase == 'initial']
	after = [item.action for item in trace if item.phase == 'recovery']
	replayed = [item.action for item in trace if item.phase == 'replay']
	reexecuted = sum((Counter(before) & Counter(after + replayed)).values())
	return TrialResult(
		mode=mode,
		scenario=scenario,
		trial=trial,
		history_actions_after_crash=history_actions,
		ledger_after_crash=ledger,
		submit_count_after_crash=crash_count,
		final_submit_count=final_count,
		final_status=status,
		task_completed=completed,
		safe_recovery=completed and final_count == 1 and not unsafe_retry,
		duplicate_effect=duplicate,
		unsafe_retry=unsafe_retry,
		actions_before_crash=len(before),
		actions_after_resume=len(after),
		replayed_actions=len(replayed),
		reexecuted_actions=reexecuted,
		recovery_duration_ms=recovery_duration_ms,
		blocked_during_outage=blocked,
	)


def _free_port() -> int:
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return int(sock.getsockname()[1])


def main() -> None:
	"""Run the configured controlled benchmark and write its reports."""
	parser = argparse.ArgumentParser(description='Final Native AgentHistory vs RecoverableHarness benchmark')
	parser.add_argument('--repeats', type=int, default=20)
	parser.add_argument('--results-dir', type=Path, default=Path(__file__).parent / 'results')
	args = parser.parse_args()
	if args.repeats < 1:
		raise ValueError('--repeats must be >= 1')
	with tempfile.TemporaryDirectory(prefix='final-recovery-comparison-') as temporary:
		workdir = Path(temporary)
		port = _free_port()
		url = f'http://127.0.0.1:{port}'
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
			for _ in range(100):
				try:
					_world(url)
					break
				except httpx.HTTPError:
					time.sleep(0.1)
			else:
				raise RuntimeError('controlled application server did not start')
			results = [
				run_trial(mode=mode, scenario=scenario, trial=trial, server_url=url, workdir=workdir)
				for scenario in Scenario
				for mode in Mode
				for trial in range(1, args.repeats + 1)
			]
			from experiments.final_comparison.report import write_reports

			write_reports(results, args.results_dir)
		finally:
			server.terminate()
			try:
				server.wait(timeout=10)
			except subprocess.TimeoutExpired:
				server.kill()
				server.wait(timeout=10)


if __name__ == '__main__':
	main()
