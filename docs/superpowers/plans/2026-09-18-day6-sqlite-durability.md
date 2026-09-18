# Day 6 SQLite Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable Contract versions, runtime checkpoints, an append-only Effect Ledger, atomic effect/checkpoint commits, and restart normalization backed by one SQLite database.

**Architecture:** New focused modules under `browser_use/recovery/persistence/` own Pydantic persistence schemas, SQLite access, checkpoint creation, and ledger operations. `browser_use/recovery/side_effects.py` provides the single side-effect boundary, while `browser_use/recovery/bootstrap.py` restores a new run from the latest consistent checkpoint without attempting Day 7 reconciliation.

**Tech Stack:** Python 3.11+, Pydantic v2, standard-library `sqlite3`, pytest, Ruff, Pyright.

**Spec:** `recoverable_runtime_MVP_docs/Day6/Day6初步代码方案.md`

## Global Constraints

- Use only Python standard-library `sqlite3`; do not introduce an ORM, migration framework, connection pool, or new dependency.
- Persist nested Contract, unit-state, evidence, and metadata values as validated JSON in SQLite `TEXT` columns.
- Keep `effect_ledger` append-only and use `INTEGER PRIMARY KEY AUTOINCREMENT` for its monotonic `seq`.
- Atomically append the effect outcome and save the corresponding checkpoint in one transaction.
- Treat `workflow_id` as stable across restarts and create a new `run_id` for each resumed process.
- Do not persist derived READY/BLOCKED state, Browser step numbers, DOM snapshots, or model reasoning.
- Do not implement Day 7 reconciliation, automatic retry, or compensation.
- Do not commit during execution: this workspace contains user-owned, ignored Day 5/Day 6 files that must remain reviewable in place.

---

### Task 1: Durable persistence schemas

**Files:**
- Create: `browser_use/recovery/persistence/models.py`
- Create: `browser_use/recovery/persistence/__init__.py`
- Test: `tests/ci/recovery/test_persistence_models.py`

**Interfaces:**
- Consumes: `FrozenModel`, `SemanticContract`, and `UnitRuntimeState` from `browser_use.recovery.contracts`.
- Produces: `EffectRecordStatus`, `EffectRecordDraft`, `EffectRecord`, `RuntimeCheckpoint`, and `WorkflowRun`.

- [ ] **Step 1: Write failing model tests**

```python
def test_checkpoint_rejects_a_state_key_that_does_not_match_unit_id() -> None:
    with pytest.raises(ValidationError, match='state key'):
        RuntimeCheckpoint(
            checkpoint_id='cp-1', workflow_id='wf-1', run_id='run-1',
            contract_id='contract-1', contract_version=1,
            unit_states={'u1': UnitRuntimeState(unit_id='u2')},
            active_unit_id=None, last_effect_seq=0,
        )

def test_effect_record_requires_positive_sequence() -> None:
    with pytest.raises(ValidationError):
        EffectRecord(
            seq=0, effect_id='effect-1', workflow_id='wf-1', run_id='run-1',
            unit_id='u1', effect_key='submit', attempt_id='attempt-1',
            status=EffectRecordStatus.PREPARED,
        )
```

- [ ] **Step 2: Run the model tests and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_persistence_models.py -q`

Expected: collection fails because `browser_use.recovery.persistence.models` does not exist.

- [ ] **Step 3: Implement immutable Pydantic v2 schemas**

Use `Field(ge=1)` for persisted sequence numbers, timezone-aware defaults, immutable copied metadata/evidence mappings, and a model validator that enforces `unit_states[key].unit_id == key` and `active_unit_id` references an in-flight state when present.

- [ ] **Step 4: Run the model tests and confirm GREEN**

Run: `uv run pytest tests/ci/recovery/test_persistence_models.py -q`

Expected: all model tests pass.

---

### Task 2: SQLite storage and transaction boundary

**Files:**
- Create: `browser_use/recovery/persistence/storage.py`
- Test: `tests/ci/recovery/test_sqlite_storage.py`

**Interfaces:**
- Consumes: Task 1 models and `SemanticContract`.
- Produces: `RuntimeStorage` protocol, `SQLiteRuntimeStorage`, `StorageConflictError`, and `StorageNotFoundError`.

- [ ] **Step 1: Write failing storage integration tests using `tmp_path`**

Cover these observable behaviors with a real SQLite database: `test_contract_versions_are_immutable_and_round_trip`, `test_effect_sequence_is_monotonic_and_history_is_append_only`, `test_latest_checkpoint_round_trips_nested_unit_states`, and `test_effect_and_checkpoint_rollback_together_when_checkpoint_insert_fails`.

The rollback test must start a valid run, append through `commit_effect_and_checkpoint`, force only the checkpoint insert to violate its Contract-version foreign key, then assert that no Effect record remains.

- [ ] **Step 2: Run storage tests and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_sqlite_storage.py -q`

Expected: collection fails because `SQLiteRuntimeStorage` does not exist.

- [ ] **Step 3: Create schema and serialization helpers**

Create `workflow_runs`, `contract_versions`, `checkpoints`, and `effect_ledger`; enable `PRAGMA foreign_keys = ON`; store schema version in `PRAGMA user_version = 1`; add indexes for latest checkpoint and per-unit ledger reads; add triggers that abort `UPDATE` and `DELETE` on `effect_ledger`.

- [ ] **Step 4: Implement storage methods**

Implement these exact public signatures: `start_run(run: WorkflowRun) -> None`, `save_contract(workflow_id: str, contract: SemanticContract) -> None`, `load_contract(workflow_id: str, contract_id: str, version: int) -> SemanticContract`, `save_checkpoint(checkpoint: RuntimeCheckpoint) -> None`, `load_latest_checkpoint(workflow_id: str) -> RuntimeCheckpoint | None`, `append_effect(draft: EffectRecordDraft) -> EffectRecord`, `read_effects(workflow_id: str, *, through_seq: int | None = None) -> tuple[EffectRecord, ...]`, `latest_effect_for_unit(workflow_id: str, unit_id: str, *, through_seq: int | None = None) -> EffectRecord | None`, and `commit_effect_and_checkpoint(draft: EffectRecordDraft, checkpoint: RuntimeCheckpoint) -> tuple[EffectRecord, RuntimeCheckpoint]`.

`commit_effect_and_checkpoint` must execute both inserts inside the same `with connection:` transaction and replace `checkpoint.last_effect_seq` with the inserted Effect sequence before saving it.

- [ ] **Step 5: Run storage tests and confirm GREEN**

Run: `uv run pytest tests/ci/recovery/test_sqlite_storage.py -q`

Expected: all storage tests pass.

---

### Task 3: Checkpoint and Effect Ledger services

**Files:**
- Create: `browser_use/recovery/persistence/checkpoint.py`
- Create: `browser_use/recovery/persistence/effect_ledger.py`
- Test: `tests/ci/recovery/test_persistence_services.py`

**Interfaces:**
- Consumes: Task 2 `RuntimeStorage` and Task 1 schemas.
- Produces: `CheckpointManager` and `EffectLedger`.

- [ ] **Step 1: Write failing service tests**

Write `test_checkpoint_manager_persists_only_stable_runtime_facts`, `test_checkpoint_manager_rejects_multiple_in_flight_units`, and `test_effect_ledger_appends_named_states_without_mutating_history`. Each test uses a real temporary SQLite database and asserts the returned Pydantic models rather than private calls.

Assert that READY/BLOCKED and Browser-step concepts are absent from the checkpoint model, rather than grepping source text.

- [ ] **Step 2: Run service tests and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_persistence_services.py -q`

- [ ] **Step 3: Implement `CheckpointManager`**

Build and save snapshots from `dict[str, UnitRuntimeState]`, derive at most one `active_unit_id` from `ACTIVE`, `COMPLETION_CANDIDATE`, or `VERIFYING`, and delegate all durable I/O to `RuntimeStorage`.

- [ ] **Step 4: Implement append-only `EffectLedger`**

Provide `append_prepared`, `append_attempted`, `append_committed`, `append_not_applied`, `append_unknown`, `records_for_unit`, and `latest_for_unit` as typed wrappers around storage. Do not expose update/delete.

- [ ] **Step 5: Run service tests and confirm GREEN**

Run: `uv run pytest tests/ci/recovery/test_persistence_services.py -q`

---

### Task 4: Atomic side-effect execution boundary

**Files:**
- Create: `browser_use/recovery/side_effects.py`
- Test: `tests/ci/recovery/test_side_effects.py`

**Interfaces:**
- Consumes: `RuntimeStateManager`, `VerificationResult`, `SQLiteRuntimeStorage`, `CheckpointManager`, and persistence models.
- Produces: `SideEffectCoordinator` and `SideEffectExecutionResult`.

- [ ] **Step 1: Write failing async behavior tests**

Write `test_verified_effect_commits_ledger_state_and_completed_checkpoint`, `test_rejected_effect_records_not_applied_and_reactivates_unit`, `test_inconclusive_effect_records_unknown_and_blocks_unit`, and `test_action_success_alone_never_marks_effect_committed`. Each test uses a real temporary SQLite database plus local async action and verification functions.

Use real storage and small local async callables; do not mock SQLite or assert mock call counts.

- [ ] **Step 2: Run side-effect tests and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_side_effects.py -q`

- [ ] **Step 3: Implement the minimal coordinator**

The coordinator must:

1. atomically append `PREPARED` with a checkpoint before invoking the action;
2. invoke the action exactly once;
3. append `ATTEMPTED` as history;
4. map structured verification outcomes through `RuntimeStateManager`;
5. atomically append `COMMITTED`, `NOT_APPLIED`, or `UNKNOWN` with the resulting checkpoint;
6. return the updated immutable state snapshot and durable records.

An action exception must produce an `UNKNOWN` final record/checkpoint and remain visible in the structured result; it must not trigger automatic retry.

- [ ] **Step 4: Run side-effect tests and confirm GREEN**

Run: `uv run pytest tests/ci/recovery/test_side_effects.py -q`

---

### Task 5: Restart normalization and bootstrap

**Files:**
- Create: `browser_use/recovery/bootstrap.py`
- Test: `tests/ci/recovery/test_recovery_bootstrap.py`

**Interfaces:**
- Consumes: Task 2 storage, persisted Contract/checkpoint/effects, and Day 5 statuses.
- Produces: `normalize_after_restart`, `RecoveryBootstrap`, and `RecoveredRuntime`.

- [ ] **Step 1: Write failing table-driven normalization tests**

Cover literal expected states for:

- `COMPLETED` and `PENDING` unchanged;
- `ACTIVE` without side effect unchanged;
- `ACTIVE + NOT_APPLIED` remains retryable `ACTIVE`;
- `ACTIVE + COMMITTED` becomes `COMPLETION_CANDIDATE` for re-verification and never re-execution;
- `ACTIVE + PREPARED/ATTEMPTED/UNKNOWN` becomes `UNKNOWN`;
- interrupted `VERIFYING` becomes `COMPLETION_CANDIDATE` for re-verification.

Add an integration test proving restore loads the exact Contract version referenced by the latest checkpoint, retains `workflow_id`, creates a new `run_id`, and does not persist derived scheduler readiness.

- [ ] **Step 2: Run bootstrap tests and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_recovery_bootstrap.py -q`

- [ ] **Step 3: Implement pure normalization and bootstrap orchestration**

`normalize_after_restart` must be deterministic and free of I/O. `RecoveryBootstrap.restore(workflow_id, run_id)` loads the checkpoint and referenced Contract, reads effects only through `last_effect_seq`, normalizes states, validates them through `RuntimeScheduler`, registers the new run with `resumed_from_checkpoint_id`, and returns `RecoveredRuntime`. It must never retry or reconcile an unknown effect.

- [ ] **Step 4: Run bootstrap tests and confirm GREEN**

Run: `uv run pytest tests/ci/recovery/test_recovery_bootstrap.py -q`

---

### Task 6: Public API and full verification

**Files:**
- Modify: `browser_use/recovery/persistence/__init__.py`
- Modify: `browser_use/recovery/__init__.py`

**Interfaces:**
- Consumes: all Day 6 public types.
- Produces: stable imports from `browser_use.recovery` and `browser_use.recovery.persistence`.

- [ ] **Step 1: Add one failing public-import test**

Add to `tests/ci/recovery/test_persistence_models.py`:

```python
def test_day6_public_api_is_importable() -> None:
    from browser_use.recovery import RecoveryBootstrap, SQLiteRuntimeStorage
    assert RecoveryBootstrap is not None
    assert SQLiteRuntimeStorage is not None
```

- [ ] **Step 2: Run the import test and confirm RED**

Run: `uv run pytest tests/ci/recovery/test_persistence_models.py::test_day6_public_api_is_importable -q`

- [ ] **Step 3: Export the Day 6 API**

Update both `__init__.py` modules without changing existing public names.

- [ ] **Step 4: Run focused and regression verification**

Run:

```powershell
uv run pytest tests/ci/recovery -q
uv run ruff check browser_use/recovery tests/ci/recovery
uv run ruff format --check browser_use/recovery tests/ci/recovery
uv run pyright browser_use/recovery
uv run pre-commit run --files browser_use/recovery/persistence/__init__.py browser_use/recovery/persistence/models.py browser_use/recovery/persistence/storage.py browser_use/recovery/persistence/checkpoint.py browser_use/recovery/persistence/effect_ledger.py browser_use/recovery/side_effects.py browser_use/recovery/bootstrap.py browser_use/recovery/__init__.py tests/ci/recovery/test_persistence_models.py tests/ci/recovery/test_sqlite_storage.py tests/ci/recovery/test_persistence_services.py tests/ci/recovery/test_side_effects.py tests/ci/recovery/test_recovery_bootstrap.py
```

Expected: all focused tests and checks pass. Any unrelated full-suite failure must be reported separately and not hidden.
