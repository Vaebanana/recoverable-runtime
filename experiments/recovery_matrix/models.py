"""Typed experiment inputs and outputs for the Day 9 recovery matrix."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ExecutionMode(StrEnum):
	BASELINE = 'baseline'
	HARNESS = 'harness'


class FaultScenario(StrEnum):
	AFTER_PREPARED = 'after_prepared'
	AFTER_ATTEMPTED = 'after_attempted'
	VERIFIER_UNAVAILABLE = 'verifier_unavailable'


class TrialResult(BaseModel):
	"""One deterministic baseline/harness trial."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	mode: ExecutionMode
	scenario: FaultScenario
	trial: int
	final_status: str
	submit_count: int
	retry_count: int
	unsafe_retry_count: int
	duplicate_effect: bool
	task_completed: bool
	safe_recovery: bool
	reconciliation_outcomes: tuple[str, ...] = ()
	notes: tuple[str, ...] = ()
