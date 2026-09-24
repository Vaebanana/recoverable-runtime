"""Native Browser Use AgentHistory baseline versus Recoverable Harness."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from browser_use.recovery.persistence import SQLiteRuntimeStorage
from browser_use.recovery.persistence.models import EffectRecordStatus
from experiments.native_history_baseline.models import CrashPoint, RecoveryMode
from experiments.native_history_baseline.worker import CRASH_EXIT_CODE
from experiments.recovery_smoke import server as smoke_server_module

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def native_history_server(tmp_path: Path):
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


async def _reset(server_url: str) -> None:
	async with httpx.AsyncClient() as client:
		response = await client.post(f'{server_url}/reset')
		response.raise_for_status()


async def _world(server_url: str) -> dict[str, object]:
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{server_url}/status')
		response.raise_for_status()
		return response.json()


def _history_actions(path: Path) -> tuple[str, ...]:
	data = json.loads(path.read_text(encoding='utf-8'))
	names: list[str] = []
	for item in data.get('history', []):
		model_output = item.get('model_output') or {}
		for action in model_output.get('action', []):
			if action:
				names.append(next(iter(action)))
	return tuple(names)


async def _worker(
	*,
	phase: str,
	mode: RecoveryMode,
	crash_point: CrashPoint,
	server_url: str,
	history_file: Path,
	runtime_db: Path,
	workflow_id: str,
	run_id: str,
) -> int:
	process = await asyncio.create_subprocess_exec(
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
		cwd=str(REPO_ROOT),
	)
	return await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'crash_point, crash_submit_count, durable_history, native_final_count, harness_ledger',
	[
		(
			CrashPoint.BEFORE_EXTERNAL_EFFECT,
			0,
			('navigate',),
			1,
			(EffectRecordStatus.PREPARED,),
		),
		(
			CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT,
			1,
			('navigate',),
			2,
			(EffectRecordStatus.PREPARED, EffectRecordStatus.ATTEMPTED),
		),
		(
			CrashPoint.AFTER_HISTORY_COMMIT,
			1,
			('navigate', 'submit_application'),
			1,
			(
				EffectRecordStatus.PREPARED,
				EffectRecordStatus.ATTEMPTED,
				EffectRecordStatus.COMMITTED,
			),
		),
	],
)
async def test_native_history_vs_harness(
	tmp_path: Path,
	native_history_server: str,
	crash_point: CrashPoint,
	crash_submit_count: int,
	durable_history: tuple[str, ...],
	native_final_count: int,
	harness_ledger: tuple[EffectRecordStatus, ...],
) -> None:
	for mode in RecoveryMode:
		await _reset(native_history_server)
		case_dir = tmp_path / f'{mode.value}-{crash_point.value}'
		case_dir.mkdir()
		history_file = case_dir / 'agent_history.json'
		runtime_db = case_dir / 'runtime.db'
		workflow_id = f'test-{mode.value}-{crash_point.value}'

		exit_code = await _worker(
			phase='crash',
			mode=mode,
			crash_point=crash_point,
			server_url=native_history_server,
			history_file=history_file,
			runtime_db=runtime_db,
			workflow_id=workflow_id,
			run_id='run-1',
		)
		assert exit_code == CRASH_EXIT_CODE
		assert _history_actions(history_file) == durable_history
		assert int(str((await _world(native_history_server))['submit_count'])) == crash_submit_count

		if mode is RecoveryMode.HARNESS:
			with SQLiteRuntimeStorage(runtime_db) as storage:
				records = storage.read_effects(workflow_id)
				assert tuple(record.status for record in records) == harness_ledger

		exit_code = await _worker(
			phase='recover',
			mode=mode,
			crash_point=crash_point,
			server_url=native_history_server,
			history_file=history_file,
			runtime_db=runtime_db,
			workflow_id=workflow_id,
			run_id='run-2',
		)
		assert exit_code == 0

		final = await _world(native_history_server)
		assert final['status'] == 'SUBMITTED'
		expected_count = native_final_count if mode is RecoveryMode.NATIVE else 1
		assert int(str(final['submit_count'])) == expected_count

		if crash_point is CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT:
			if mode is RecoveryMode.NATIVE:
				assert int(str(final['submit_count'])) == 2
			else:
				assert int(str(final['submit_count'])) == 1
