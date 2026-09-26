"""Per-scenario and aggregate reports for the controlled recovery benchmark."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from experiments.final_comparison.models import BenchmarkSummary, Mode, RateSummary, Scenario, TrialResult


def _rates(results: list[TrialResult]) -> RateSummary:
	count = len(results)
	return RateSummary(
		trials=count,
		task_completion_rate=100 * sum(item.task_completed for item in results) / count,
		safe_recovery_rate=100 * sum(item.safe_recovery for item in results) / count,
		duplicate_effect_rate=100 * sum(item.duplicate_effect for item in results) / count,
		unsafe_retry_rate=100 * sum(item.unsafe_retry for item in results) / count,
		mean_actions_before_crash=sum(item.actions_before_crash for item in results) / count,
		mean_actions_after_resume=sum(item.actions_after_resume for item in results) / count,
		mean_replayed_actions=sum(item.replayed_actions for item in results) / count,
		mean_reexecuted_actions=sum(item.reexecuted_actions for item in results) / count,
		mean_recovery_duration_ms=sum(item.recovery_duration_ms for item in results) / count,
	)


def summarize(results: list[TrialResult]) -> BenchmarkSummary:
	"""Compute primary correctness rates before secondary action/time costs."""
	if not results:
		raise ValueError('at least one trial is required for a summary')
	per_scenario = {
		scenario.value: {
			mode.value: _rates(group)
			for mode in Mode
			if (group := [item for item in results if item.scenario is scenario and item.mode is mode])
		}
		for scenario in Scenario
		if any(item.scenario is scenario for item in results)
	}
	aggregate = {mode.value: _rates(group) for mode in Mode if (group := [item for item in results if item.mode is mode])}
	return BenchmarkSummary(
		experiment='native_agent_history_vs_recoverable_harness',
		generated_at=datetime.now(timezone.utc).isoformat(),
		caveat='Crash scenarios are equally weighted controlled cases, not estimates of production crash probabilities.',
		per_scenario=per_scenario,
		aggregate=aggregate,
	)


def write_reports(results: list[TrialResult], results_dir: Path) -> None:
	"""Write raw trials, machine-readable summary, and a scenario-first Markdown report."""
	results_dir.mkdir(parents=True, exist_ok=True)
	summary = summarize(results)
	(results_dir / 'raw_trials.json').write_text(
		json.dumps([item.model_dump(mode='json') for item in results], indent=2, ensure_ascii=False) + '\n',
		encoding='utf-8',
	)
	(results_dir / 'summary.json').write_text(summary.model_dump_json(indent=2) + '\n', encoding='utf-8')
	per_scenario = summary.per_scenario
	aggregate = summary.aggregate
	lines = [
		'# Final Recovery Benchmark',
		'',
		'Native AgentHistory vs RecoverableHarness on a non-idempotent browser submit.',
		'',
		summary.caveat,
		'',
		'| Scenario | Mode | Trials | Completion % | Safe recovery % | Duplicate % | Unsafe retry % |',
		'| --- | --- | ---: | ---: | ---: | ---: | ---: |',
	]
	for scenario in Scenario:
		groups = per_scenario.get(scenario.value, {})
		for mode in Mode:
			stats = groups.get(mode.value)
			if stats is None:
				continue
			lines.append(
				f'| {scenario.value} | {mode.value} | {stats.trials} | '
				f'{stats.task_completion_rate:.1f} | {stats.safe_recovery_rate:.1f} | '
				f'{stats.duplicate_effect_rate:.1f} | {stats.unsafe_retry_rate:.1f} |'
			)
	lines.extend(
		[
			'',
			'## Aggregate',
			'',
			'| Mode | Trials | Completion % | Safe recovery % | Duplicate % | Unsafe retry % |',
			'| --- | ---: | ---: | ---: | ---: | ---: |',
		]
	)
	for mode in Mode:
		stats = aggregate.get(mode.value)
		if stats is None:
			continue
		lines.append(
			f'| {mode.value} | {stats.trials} | {stats.task_completion_rate:.1f} | '
			f'{stats.safe_recovery_rate:.1f} | {stats.duplicate_effect_rate:.1f} | '
			f'{stats.unsafe_retry_rate:.1f} |'
		)
	lines.extend(
		[
			'',
			'## Recovery cost (secondary)',
			'',
			'| Mode | Before crash | After resume | Replayed | Reexecuted | Recovery ms |',
			'| --- | ---: | ---: | ---: | ---: | ---: |',
		]
	)
	for mode in Mode:
		stats = aggregate.get(mode.value)
		if stats is None:
			continue
		lines.append(
			f'| {mode.value} | {stats.mean_actions_before_crash:.2f} | {stats.mean_actions_after_resume:.2f} | '
			f'{stats.mean_replayed_actions:.2f} | {stats.mean_reexecuted_actions:.2f} | '
			f'{stats.mean_recovery_duration_ms:.1f} |'
		)
	(results_dir / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
