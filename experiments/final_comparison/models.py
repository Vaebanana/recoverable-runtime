"""Validated scenario and result schemas for the final recovery benchmark."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Mode(StrEnum):
	"""The two Browser Use recovery strategies under comparison."""

	NATIVE = 'native'
	HARNESS = 'harness'


class Scenario(StrEnum):
	"""Controlled process stop relative to the external submit and durable facts."""

	NORMAL = 'normal'
	BEFORE_EFFECT = 'before_effect'
	AFTER_EFFECT_BEFORE_ATTEMPTED = 'after_effect_before_attempted'
	AFTER_ATTEMPTED_BEFORE_HISTORY = 'after_attempted_before_history'
	AFTER_HISTORY_COMMIT = 'after_history_commit'
	VERIFIER_UNAVAILABLE = 'verifier_unavailable'


class TrialResult(BaseModel):
	"""Externally scored result and measured action cost of one process trial."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	mode: Mode
	scenario: Scenario
	trial: int = Field(ge=1)
	history_actions_after_crash: tuple[str, ...]
	ledger_after_crash: tuple[str, ...]
	submit_count_after_crash: int = Field(ge=0)
	final_submit_count: int = Field(ge=0)
	final_status: str
	task_completed: bool
	safe_recovery: bool
	duplicate_effect: bool
	unsafe_retry: bool
	actions_before_crash: int = Field(ge=0)
	actions_after_resume: int = Field(ge=0)
	replayed_actions: int = Field(ge=0)
	reexecuted_actions: int = Field(ge=0)
	recovery_duration_ms: float = Field(ge=0)
	blocked_during_outage: bool = False


class ActionTrace(BaseModel):
	"""One fsynced native or replay action observed by a worker."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	phase: str
	action: str


class RateSummary(BaseModel):
	"""Correctness rates and secondary mean recovery costs for one group."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	trials: int = Field(ge=1)
	task_completion_rate: float
	safe_recovery_rate: float
	duplicate_effect_rate: float
	unsafe_retry_rate: float
	mean_actions_before_crash: float
	mean_actions_after_resume: float
	mean_replayed_actions: float
	mean_reexecuted_actions: float
	mean_recovery_duration_ms: float


class BenchmarkSummary(BaseModel):
	"""Machine-readable scenario-first report over controlled trial groups."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	experiment: str
	generated_at: str
	caveat: str
	per_scenario: dict[str, dict[str, RateSummary]]
	aggregate: dict[str, RateSummary]
