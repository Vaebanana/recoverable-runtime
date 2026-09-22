"""Persist raw Day 9 results and render a compact comparison report."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from experiments.recovery_matrix.models import ExecutionMode, TrialResult


def _rate(numerator: int, denominator: int) -> float:
	return 0.0 if denominator == 0 else numerator / denominator


def summarize(results: list[TrialResult]) -> dict[str, dict[str, float | int]]:
	"""Aggregate trial-level outcomes by execution mode."""
	grouped: dict[ExecutionMode, list[TrialResult]] = defaultdict(list)
	for result in results:
		grouped[result.mode].append(result)

	summary: dict[str, dict[str, float | int]] = {}
	for mode in ExecutionMode:
		items = grouped.get(mode, [])
		n = len(items)
		summary[mode.value] = {
			'trials': n,
			'task_completion_rate': _rate(sum(item.task_completed for item in items), n),
			'safe_recovery_rate': _rate(sum(item.safe_recovery for item in items), n),
			'duplicate_effect_rate': _rate(sum(item.duplicate_effect for item in items), n),
			'unsafe_retry_rate': _rate(sum(item.unsafe_retry_count > 0 for item in items), n),
		}
	return summary


def render_markdown(results: list[TrialResult]) -> str:
	"""Render raw scenario outcomes plus aggregate metrics."""
	summary = summarize(results)
	lines = [
		'# Recovery Matrix Results',
		'',
		'## Scenario results',
		'',
		'| Mode | Scenario | Trial | Final status | Submit count | Duplicate | Unsafe retries | Reconciliation | Safe recovery |',
		'| --- | --- | ---: | --- | ---: | --- | ---: | --- | --- |',
	]
	for result in results:
		outcomes = ' → '.join(result.reconciliation_outcomes) if result.reconciliation_outcomes else '—'
		lines.append(
			f'| {result.mode.value} | {result.scenario.value} | {result.trial} | '
			f'{result.final_status} | {result.submit_count} | {"yes" if result.duplicate_effect else "no"} | '
			f'{result.unsafe_retry_count} | {outcomes} | {"yes" if result.safe_recovery else "no"} |'
		)

	lines.extend(
		[
			'',
			'## Aggregate metrics',
			'',
			'| Metric | Baseline | Harness |',
			'| --- | ---: | ---: |',
		]
	)
	for key, label in [
		('task_completion_rate', 'Task Completion Rate'),
		('safe_recovery_rate', 'Safe Recovery Rate'),
		('duplicate_effect_rate', 'Duplicate Effect Rate'),
		('unsafe_retry_rate', 'Unsafe Retry Rate'),
	]:
		baseline = float(summary[ExecutionMode.BASELINE.value][key])
		harness = float(summary[ExecutionMode.HARNESS.value][key])
		lines.append(f'| {label} | {baseline:.1%} | {harness:.1%} |')

	lines.extend(
		[
			'',
			'## Metric definitions',
			'',
			'- **Task Completion Rate**: final external application status is `SUBMITTED`.',
			'- **Safe Recovery Rate**: task completed with exactly one external submit and no unsafe retry.',
			'- **Duplicate Effect Rate**: `submit_count > 1`.',
			'- **Unsafe Retry Rate**: a new external effect was executed while the prior effect outcome remained uncertain.',
			'',
			'> The baseline is restart-from-task without durable effect history, safety gates, or reconciliation. '
			'This is an experimental comparison baseline, not a claim about every possible Browser Use recovery strategy.',
		]
	)
	return '\n'.join(lines) + '\n'


def write_results(output_dir: Path, results: list[TrialResult]) -> tuple[Path, Path]:
	"""Write machine-readable and interview-friendly reports to stable paths."""
	output_dir.mkdir(parents=True, exist_ok=True)
	json_path = output_dir / 'latest.json'
	markdown_path = output_dir / 'latest.md'
	payload = {
		'results': [result.model_dump(mode='json') for result in results],
		'summary': summarize(results),
	}
	json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
	markdown_path.write_text(render_markdown(results), encoding='utf-8')
	return json_path, markdown_path
