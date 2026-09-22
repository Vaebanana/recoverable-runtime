"""Day 9 deterministic baseline-vs-harness recovery matrix."""

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from browser_use.browser.session import BrowserSession
from experiments.recovery_matrix.baseline import run_baseline_trial
from experiments.recovery_matrix.harness import run_harness_trial
from experiments.recovery_matrix.models import FaultScenario
from experiments.recovery_smoke import server as smoke_server_module


@pytest.fixture
async def matrix_server(tmp_path: Path):
	"""Run the controllable application server with independent SQLite state."""
	smoke_server_module._db_path = tmp_path / 'state.db'
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'scenario, baseline_submit_count',
	[
		(FaultScenario.AFTER_PREPARED, 1),
		(FaultScenario.AFTER_ATTEMPTED, 2),
		(FaultScenario.VERIFIER_UNAVAILABLE, 2),
	],
)
async def test_baseline_and_harness_fault_matrix(
	tmp_path: Path,
	matrix_server: str,
	browser_session: BrowserSession,
	scenario: FaultScenario,
	baseline_submit_count: int,
) -> None:
	baseline = await run_baseline_trial(
		scenario=scenario,
		trial=1,
		server_url=matrix_server,
		browser_session=browser_session,
	)
	assert baseline.task_completed is True
	assert baseline.submit_count == baseline_submit_count
	assert baseline.duplicate_effect is (baseline_submit_count > 1)

	harness = await run_harness_trial(
		scenario=scenario,
		trial=1,
		server_url=matrix_server,
		browser_session=browser_session,
		runtime_db=tmp_path / f'{scenario.value}.db',
	)
	assert harness.task_completed is True
	assert harness.safe_recovery is True
	assert harness.submit_count == 1
	assert harness.duplicate_effect is False
	assert harness.unsafe_retry_count == 0

	if scenario is FaultScenario.AFTER_PREPARED:
		assert harness.reconciliation_outcomes == ('rejected',)
		assert harness.retry_count == 1
	elif scenario is FaultScenario.AFTER_ATTEMPTED:
		assert harness.reconciliation_outcomes == ('verified',)
		assert harness.retry_count == 0
	else:
		assert harness.reconciliation_outcomes == ('inconclusive', 'verified')
		assert harness.retry_count == 0
