"""SQLite durable storage for Recoverable Runtime state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from browser_use.recovery.contracts import SemanticContract, UnitRuntimeState
from browser_use.recovery.persistence.models import (
	EffectRecord,
	EffectRecordDraft,
	RuntimeCheckpoint,
	WorkflowRun,
)

SCHEMA_VERSION = 1


class StorageError(RuntimeError):
	"""Base error for durable Runtime storage failures."""


class StorageConflictError(StorageError):
	"""Raised when a write conflicts with immutable durable state."""


class StorageNotFoundError(StorageError):
	"""Raised when required durable state does not exist."""


class RuntimeStorage(Protocol):
	"""Storage boundary used by Runtime persistence services."""

	def start_run(self, run: WorkflowRun) -> None:
		"""Persist one process run."""
		...

	def save_contract(self, workflow_id: str, contract: SemanticContract) -> None:
		"""Persist one immutable Contract version."""
		...

	def load_contract(self, workflow_id: str, contract_id: str, version: int) -> SemanticContract:
		"""Load one exact Contract version."""
		...

	def save_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
		"""Append one Runtime checkpoint."""
		...

	def load_latest_checkpoint(self, workflow_id: str) -> RuntimeCheckpoint | None:
		"""Load the latest checkpoint for a workflow."""
		...

	def append_effect(self, draft: EffectRecordDraft) -> EffectRecord:
		"""Append one immutable Effect Ledger fact."""
		...

	def read_effects(self, workflow_id: str, *, through_seq: int | None = None) -> tuple[EffectRecord, ...]:
		"""Read ordered Effect Ledger facts for a workflow."""
		...

	def latest_effect_for_unit(
		self,
		workflow_id: str,
		unit_id: str,
		*,
		through_seq: int | None = None,
	) -> EffectRecord | None:
		"""Read the latest durable effect fact for one unit."""
		...

	def commit_effect_and_checkpoint(
		self,
		draft: EffectRecordDraft,
		checkpoint: RuntimeCheckpoint,
	) -> tuple[EffectRecord, RuntimeCheckpoint]:
		"""Atomically append an effect and its corresponding checkpoint."""
		...


class SQLiteRuntimeStorage:
	"""Small SQLite implementation with explicit transactional boundaries."""

	def __init__(self, database_path: str | Path) -> None:
		self._database_path = Path(database_path)
		self._connection = sqlite3.connect(self._database_path)
		self._connection.row_factory = sqlite3.Row
		self._connection.execute('PRAGMA foreign_keys = ON')
		self._initialize_schema()

	def __enter__(self) -> Self:
		"""Return this open storage for a context manager."""
		return self

	def __exit__(
		self,
		exc_type: type[BaseException] | None,
		exc_value: BaseException | None,
		traceback: TracebackType | None,
	) -> None:
		"""Close the owned SQLite connection."""
		self.close()

	def close(self) -> None:
		"""Close the owned SQLite connection."""
		self._connection.close()

	def start_run(self, run: WorkflowRun) -> None:
		"""Persist a run once, accepting an identical idempotent repeat."""
		row = self._connection.execute(
			'SELECT started_at, resumed_from_checkpoint_id FROM workflow_runs WHERE workflow_id = ? AND run_id = ?',
			(run.workflow_id, run.run_id),
		).fetchone()
		values = (run.started_at.isoformat(), run.resumed_from_checkpoint_id)
		if row is not None:
			if (row['started_at'], row['resumed_from_checkpoint_id']) != values:
				raise StorageConflictError(f'run identity is immutable: {run.workflow_id}/{run.run_id}')
			return
		try:
			with self._connection:
				self._connection.execute(
					'INSERT INTO workflow_runs (workflow_id, run_id, started_at, resumed_from_checkpoint_id) VALUES (?, ?, ?, ?)',
					(run.workflow_id, run.run_id, *values),
				)
		except sqlite3.IntegrityError as exc:
			raise StorageConflictError(f'cannot persist run {run.workflow_id}/{run.run_id}') from exc

	def save_contract(self, workflow_id: str, contract: SemanticContract) -> None:
		"""Persist an immutable version, accepting an identical repeat."""
		contract_json = contract.model_dump_json()
		row = self._connection.execute(
			'SELECT contract_json FROM contract_versions WHERE workflow_id = ? AND contract_id = ? AND version = ?',
			(workflow_id, contract.contract_id, contract.version),
		).fetchone()
		if row is not None:
			if row['contract_json'] != contract_json:
				raise StorageConflictError(
					f'Contract version is immutable: {workflow_id}/{contract.contract_id}/v{contract.version}'
				)
			return
		try:
			with self._connection:
				self._connection.execute(
					'INSERT INTO contract_versions '
					'(workflow_id, contract_id, version, contract_json, created_at) VALUES (?, ?, ?, ?, ?)',
					(
						workflow_id,
						contract.contract_id,
						contract.version,
						contract_json,
						contract.created_at.isoformat(),
					),
				)
		except sqlite3.IntegrityError as exc:
			raise StorageConflictError(
				f'cannot persist Contract version {workflow_id}/{contract.contract_id}/v{contract.version}'
			) from exc

	def load_contract(self, workflow_id: str, contract_id: str, version: int) -> SemanticContract:
		"""Load and validate one exact immutable Contract version."""
		row = self._connection.execute(
			'SELECT contract_json FROM contract_versions WHERE workflow_id = ? AND contract_id = ? AND version = ?',
			(workflow_id, contract_id, version),
		).fetchone()
		if row is None:
			raise StorageNotFoundError(f'Contract not found: {workflow_id}/{contract_id}/v{version}')
		return SemanticContract.model_validate_json(row['contract_json'])

	def save_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
		"""Append one validated checkpoint without overwriting history."""
		try:
			with self._connection:
				self._insert_checkpoint(checkpoint)
		except sqlite3.IntegrityError as exc:
			raise StorageConflictError(f'cannot persist checkpoint {checkpoint.checkpoint_id}') from exc

	def load_latest_checkpoint(self, workflow_id: str) -> RuntimeCheckpoint | None:
		"""Load the checkpoint with the greatest insertion sequence."""
		row = self._connection.execute(
			'SELECT * FROM checkpoints WHERE workflow_id = ? ORDER BY checkpoint_seq DESC LIMIT 1',
			(workflow_id,),
		).fetchone()
		return None if row is None else self._checkpoint_from_row(row)

	def append_effect(self, draft: EffectRecordDraft) -> EffectRecord:
		"""Append one effect fact in its own transaction."""
		try:
			with self._connection:
				return self._insert_effect(draft)
		except sqlite3.IntegrityError as exc:
			raise StorageConflictError(f'cannot append effect {draft.effect_id}') from exc

	def read_effects(self, workflow_id: str, *, through_seq: int | None = None) -> tuple[EffectRecord, ...]:
		"""Read ordered effect history, optionally bounded by a checkpoint position."""
		query = 'SELECT * FROM effect_ledger WHERE workflow_id = ?'
		parameters: list[object] = [workflow_id]
		if through_seq is not None:
			query += ' AND seq <= ?'
			parameters.append(through_seq)
		query += ' ORDER BY seq ASC'
		rows = self._connection.execute(query, parameters).fetchall()
		return tuple(self._effect_from_row(row) for row in rows)

	def latest_effect_for_unit(
		self,
		workflow_id: str,
		unit_id: str,
		*,
		through_seq: int | None = None,
	) -> EffectRecord | None:
		"""Read the latest effect fact for one unit at a durable position."""
		query = 'SELECT * FROM effect_ledger WHERE workflow_id = ? AND unit_id = ?'
		parameters: list[object] = [workflow_id, unit_id]
		if through_seq is not None:
			query += ' AND seq <= ?'
			parameters.append(through_seq)
		query += ' ORDER BY seq DESC LIMIT 1'
		row = self._connection.execute(query, parameters).fetchone()
		return None if row is None else self._effect_from_row(row)

	def commit_effect_and_checkpoint(
		self,
		draft: EffectRecordDraft,
		checkpoint: RuntimeCheckpoint,
	) -> tuple[EffectRecord, RuntimeCheckpoint]:
		"""Atomically append an effect fact and a checkpoint pointing to it."""
		if (draft.workflow_id, draft.run_id) != (checkpoint.workflow_id, checkpoint.run_id):
			raise StorageConflictError('effect and checkpoint must belong to the same workflow run')
		try:
			with self._connection:
				record = self._insert_effect(draft)
				updated_checkpoint = checkpoint.model_copy(update={'last_effect_seq': record.seq})
				self._insert_checkpoint(updated_checkpoint)
		except sqlite3.IntegrityError as exc:
			raise StorageConflictError('atomic effect/checkpoint commit failed') from exc
		return record, updated_checkpoint

	def _initialize_schema(self) -> None:
		"""Create the single supported schema without a migration framework."""
		current_version = int(self._connection.execute('PRAGMA user_version').fetchone()[0])
		if current_version not in {0, SCHEMA_VERSION}:
			raise StorageConflictError(f'unsupported Runtime database schema {current_version}; expected {SCHEMA_VERSION}')
		with self._connection:
			self._connection.executescript(
				"""
				CREATE TABLE IF NOT EXISTS workflow_runs (
					workflow_id TEXT NOT NULL,
					run_id TEXT NOT NULL,
					started_at TEXT NOT NULL,
					resumed_from_checkpoint_id TEXT,
					PRIMARY KEY (workflow_id, run_id)
				);

				CREATE TABLE IF NOT EXISTS contract_versions (
					workflow_id TEXT NOT NULL,
					contract_id TEXT NOT NULL,
					version INTEGER NOT NULL CHECK (version >= 1),
					contract_json TEXT NOT NULL,
					created_at TEXT NOT NULL,
					PRIMARY KEY (workflow_id, contract_id, version)
				);

				CREATE TABLE IF NOT EXISTS checkpoints (
					checkpoint_seq INTEGER PRIMARY KEY AUTOINCREMENT,
					checkpoint_id TEXT NOT NULL UNIQUE,
					workflow_id TEXT NOT NULL,
					run_id TEXT NOT NULL,
					contract_id TEXT NOT NULL,
					contract_version INTEGER NOT NULL,
					unit_states_json TEXT NOT NULL,
					active_unit_id TEXT,
					last_effect_seq INTEGER NOT NULL CHECK (last_effect_seq >= 0),
					metadata_json TEXT NOT NULL,
					created_at TEXT NOT NULL,
					FOREIGN KEY (workflow_id, run_id)
						REFERENCES workflow_runs (workflow_id, run_id),
					FOREIGN KEY (workflow_id, contract_id, contract_version)
						REFERENCES contract_versions (workflow_id, contract_id, version)
				);

				CREATE TABLE IF NOT EXISTS effect_ledger (
					seq INTEGER PRIMARY KEY AUTOINCREMENT,
					effect_id TEXT NOT NULL,
					workflow_id TEXT NOT NULL,
					run_id TEXT NOT NULL,
					unit_id TEXT NOT NULL,
					effect_key TEXT NOT NULL,
					attempt_id TEXT NOT NULL,
					status TEXT NOT NULL,
					idempotency_key TEXT,
					evidence_json TEXT,
					metadata_json TEXT NOT NULL,
					created_at TEXT NOT NULL,
					FOREIGN KEY (workflow_id, run_id)
						REFERENCES workflow_runs (workflow_id, run_id)
				);

				CREATE INDEX IF NOT EXISTS checkpoints_by_workflow
					ON checkpoints (workflow_id, checkpoint_seq DESC);
				CREATE INDEX IF NOT EXISTS effects_by_workflow_unit
					ON effect_ledger (workflow_id, unit_id, seq DESC);

				CREATE TRIGGER IF NOT EXISTS effect_ledger_no_update
				BEFORE UPDATE ON effect_ledger
				BEGIN
					SELECT RAISE(ABORT, 'effect_ledger is append-only');
				END;

				CREATE TRIGGER IF NOT EXISTS effect_ledger_no_delete
				BEFORE DELETE ON effect_ledger
				BEGIN
					SELECT RAISE(ABORT, 'effect_ledger is append-only');
				END;
				"""
			)
			self._connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

	def _insert_effect(self, draft: EffectRecordDraft) -> EffectRecord:
		"""Insert one draft on the caller's active transaction."""
		cursor = self._connection.execute(
			'INSERT INTO effect_ledger '
			'(effect_id, workflow_id, run_id, unit_id, effect_key, attempt_id, status, '
			'idempotency_key, evidence_json, metadata_json, created_at) '
			'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
			(
				draft.effect_id,
				draft.workflow_id,
				draft.run_id,
				draft.unit_id,
				draft.effect_key,
				draft.attempt_id,
				draft.status.value,
				draft.idempotency_key,
				_json_dump(draft.evidence) if draft.evidence is not None else None,
				_json_dump(draft.metadata),
				draft.created_at.isoformat(),
			),
		)
		assert cursor.lastrowid is not None
		return EffectRecord(seq=cursor.lastrowid, **draft.model_dump())

	def _insert_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
		"""Insert one checkpoint on the caller's active transaction."""
		unit_states = {unit_id: state.model_dump(mode='json') for unit_id, state in checkpoint.unit_states.items()}
		self._connection.execute(
			'INSERT INTO checkpoints '
			'(checkpoint_id, workflow_id, run_id, contract_id, contract_version, unit_states_json, '
			'active_unit_id, last_effect_seq, metadata_json, created_at) '
			'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
			(
				checkpoint.checkpoint_id,
				checkpoint.workflow_id,
				checkpoint.run_id,
				checkpoint.contract_id,
				checkpoint.contract_version,
				_json_dump(unit_states),
				checkpoint.active_unit_id,
				checkpoint.last_effect_seq,
				_json_dump(checkpoint.metadata),
				checkpoint.created_at.isoformat(),
			),
		)

	@staticmethod
	def _effect_from_row(row: sqlite3.Row) -> EffectRecord:
		"""Validate one SQLite row as an Effect record."""
		return EffectRecord.model_validate(
			{
				'seq': row['seq'],
				'effect_id': row['effect_id'],
				'workflow_id': row['workflow_id'],
				'run_id': row['run_id'],
				'unit_id': row['unit_id'],
				'effect_key': row['effect_key'],
				'attempt_id': row['attempt_id'],
				'status': row['status'],
				'idempotency_key': row['idempotency_key'],
				'evidence': _json_load(row['evidence_json']),
				'metadata': _json_load(row['metadata_json']),
				'created_at': row['created_at'],
			}
		)

	@staticmethod
	def _checkpoint_from_row(row: sqlite3.Row) -> RuntimeCheckpoint:
		"""Validate one SQLite row as a Runtime checkpoint."""
		state_values = _json_load(row['unit_states_json'])
		assert isinstance(state_values, dict)
		return RuntimeCheckpoint.model_validate(
			{
				'checkpoint_id': row['checkpoint_id'],
				'workflow_id': row['workflow_id'],
				'run_id': row['run_id'],
				'contract_id': row['contract_id'],
				'contract_version': row['contract_version'],
				'unit_states': {unit_id: UnitRuntimeState.model_validate(state) for unit_id, state in state_values.items()},
				'active_unit_id': row['active_unit_id'],
				'last_effect_seq': row['last_effect_seq'],
				'metadata': _json_load(row['metadata_json']),
				'created_at': row['created_at'],
			}
		)


def _json_dump(value: Mapping[str, object] | dict[str, object]) -> str:
	"""Serialize a mapping deterministically for durable TEXT storage."""
	return json.dumps(dict(value), ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _json_load(value: str | None) -> object:
	"""Deserialize one nullable JSON TEXT value."""
	return None if value is None else json.loads(value)
