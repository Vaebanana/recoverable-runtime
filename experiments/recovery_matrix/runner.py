"""Run the Day 9 baseline-vs-harness fault matrix against the local smoke server.

Prerequisite in another terminal:

    uv run python experiments/recovery_smoke/server.py --port 8765

Run one deterministic matrix:

    uv run python -m experiments.recovery_matrix.runner --headless

Repeat each mode/scenario N times:

    uv run python -m experiments.recovery_matrix.runner --headless --trials 5

Results are written to:

    experiments/recovery_matrix/results/latest.json
    experiments/recovery_matrix/results/latest.md
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from browser_use.browser import BrowserProfile, BrowserSession
from experiments.recovery_matrix.baseline import run_baseline_trial
from experiments.recovery_matrix.harness import run_harness_trial
from experiments.recovery_matrix.models import FaultScenario, TrialResult
from experiments.recovery_matrix.report import render_markdown, write_results

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / 'results'


async def _start_browser(headless: bool) -> BrowserSession:
	browser_session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=headless,
			user_data_dir=None,
			keep_alive=True,
		)
	)
	await browser_session.start()
	return browser_session


async def run_matrix(*, server_url: str, trials: int, headless: bool) -> list[TrialResult]:
	if trials < 1:
		raise ValueError('trials must be >= 1')

	results: list[TrialResult] = []
	browser_session = await _start_browser(headless)
	try:
		with tempfile.TemporaryDirectory(prefix='recoverable-runtime-matrix-') as runtime_root:
			root = Path(runtime_root)
			for trial in range(1, trials + 1):
				for scenario in FaultScenario:
					baseline = await run_baseline_trial(
						scenario=scenario,
						trial=trial,
						server_url=server_url,
						browser_session=browser_session,
					)
					results.append(baseline)
					print(
						f'[baseline] {scenario.value} trial={trial}: '
						f'submit_count={baseline.submit_count} safe={baseline.safe_recovery}'
					)

					harness = await run_harness_trial(
						scenario=scenario,
						trial=trial,
						server_url=server_url,
						browser_session=browser_session,
						runtime_db=root / f'{scenario.value}-{trial}.db',
					)
					results.append(harness)
					print(
						f'[harness]  {scenario.value} trial={trial}: '
						f'submit_count={harness.submit_count} safe={harness.safe_recovery} '
						f'reconcile={harness.reconciliation_outcomes}'
					)
	finally:
		await browser_session.kill()
	return results


def main() -> None:
	parser = argparse.ArgumentParser(description='Run baseline/harness recovery fault matrix.')
	parser.add_argument('--url', default='http://127.0.0.1:8765', help='Recovery smoke server URL')
	parser.add_argument('--trials', type=int, default=1, help='Repetitions per mode/scenario (default: 1)')
	parser.add_argument('--headless', action='store_true', help='Run Chromium headless')
	parser.add_argument('--output-dir', default=str(DEFAULT_RESULTS_DIR), help='Directory for latest.json/latest.md')
	args = parser.parse_args()

	results = asyncio.run(run_matrix(server_url=args.url, trials=args.trials, headless=args.headless))
	json_path, markdown_path = write_results(Path(args.output_dir), results)
	print()
	print(render_markdown(results))
	print(f'JSON results: {json_path}')
	print(f'Markdown report: {markdown_path}')


if __name__ == '__main__':
	main()
