"""Typed inputs and outputs for the Native Browser Use history baseline."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RecoveryMode(StrEnum):
	"""Recovery capability being evaluated."""

	NATIVE = 'native'
	HARNESS = 'harness'


class CrashPoint(StrEnum):
	"""System-neutral crash points shared by Native and Harness runs."""

	BEFORE_EXTERNAL_EFFECT = 'before_external_effect'
	AFTER_EXTERNAL_EFFECT_BEFORE_HISTORY_COMMIT = 'after_external_effect_before_history_commit'
	AFTER_HISTORY_COMMIT = 'after_history_commit'


class TrialResult(BaseModel):
	"""One deterministic Native-vs-Harness recovery trial."""

	model_config = ConfigDict(frozen=True, extra='forbid')

	mode: RecoveryMode
	crash_point: CrashPoint
	trial: int
	history_actions_after_crash: tuple[str, ...]
	submit_count_after_crash: int
	final_submit_count: int
	final_status: str
	task_completed: bool
	duplicate_effect: bool
	unsafe_retry: bool
	safe_recovery: bool
	ledger_after_crash: tuple[str, ...] = ()
	notes: tuple[str, ...] = ()
