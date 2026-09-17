# Recoverable Contract Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Day 5 Semantic Contract core covering schema boundaries, stable identity and immutable versioning, atomic Delta updates, and the minimal runtime-state/scheduling support required by those rules.

**Architecture:** Add a self-contained `browser_use.recovery` package. Frozen Pydantic v2 models describe semantic definitions and mutable-in-time runtime snapshots; `ContractManager` is the only authority that assigns persistent IDs or creates a new contract version; `RuntimeStateManager` and `RuntimeScheduler` keep execution state outside contract snapshots.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest 9, Ruff, uv

**Spec:** `recoverable_runtime_MVP_docs/Day5/Contract 的职责边界和第一版 Schema.md`, `recoverable_runtime_MVP_docs/Day5/Versioning规则.md`, and `recoverable_runtime_MVP_docs/Day5/代码实施参考.md`

## Global Constraints

- Use `uv` for every Python command.
- Use Pydantic v2 models for all Contract, operation, and runtime-state I/O.
- Runtime assigns authoritative `contract_id` and `unit_id`; an LLM may only provide a temporary reference or suggest an existing candidate ID.
- Contract snapshots are immutable; semantic changes create `version + 1`, while runtime-state changes do not change the Contract version.
- Delta operations are limited to `ADD_UNIT`, `UPDATE_CONSTRAINTS`, and `SUPERSEDE_UNIT`; no delete or identity overwrite operation exists.
- Delta application validates `contract_id` and `base_version` and is atomic from the caller's perspective.
- Exact normalized fingerprints may deduplicate; fuzzy matching is intentionally out of scope.
- Checkpoint persistence, Effect Ledger, Reconciliation, Completion Verification, Browser hooks, and action interception are out of scope.

---

### Task 1: Contract and Runtime-State Schemas

**Files:**
- Create: `browser_use/recovery/contracts.py`
- Create: `browser_use/recovery/__init__.py`
- Create: `tests/ci/recovery/test_contract_core.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, `ConfigDict`, `Field`, and discriminated unions.
- Produces: `SemanticContract`, `SemanticUnit`, `UnitProposal`, `ProposedUnit`, `UnitReference`, `UnitIdentity`, `UnitRuntimeState`, `ContractDelta`, its three operation models, supporting enums/specs, and `ContractApplyResult`.

- [x] **Step 1: Write failing schema tests**

  Add tests that construct the four public data families, verify tuple coercion and defaults, reject unknown fields, reject invalid `UnitReference` values, and prove frozen contract models cannot be assigned in place.

- [x] **Step 2: Run schema tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: collection fails with `ModuleNotFoundError: No module named 'browser_use.recovery'`.

- [x] **Step 3: Implement the minimal schemas**

  Implement string enums for idempotency, reversibility, verification source, unit status, verification status, and effect status. Implement frozen, extra-forbidden Pydantic models and a discriminated `DeltaOperation` union keyed by `op`.

- [x] **Step 4: Run schema tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: schema tests pass with no warnings.

### Task 2: Stable Identity, Initial Contracts, and Contract Validation

**Files:**
- Create: `browser_use/recovery/manager.py`
- Modify: `browser_use/recovery/__init__.py`
- Modify: `tests/ci/recovery/test_contract_core.py`

**Interfaces:**
- Consumes: `ProposedUnit`, `UnitProposal`, `UnitReference`, and frozen Contract models from Task 1.
- Produces: `ContractManager.create_initial_contract(task_id: str, proposed_units: list[ProposedUnit]) -> SemanticContract`, `resolve_proposal(contract, proposal) -> SemanticUnit | None`, and typed validation errors.

- [x] **Step 1: Write failing identity and validation tests**

  Cover runtime ID assignment, temporary-reference dependency resolution (including forward references in an initial proposal), exact normalized fingerprint deduplication, explicit candidate mismatch rejection, missing dependency rejection, duplicate IDs/fingerprints, self-dependency, and cycle rejection.

- [x] **Step 2: Run identity tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: imports or assertions fail because `ContractManager` does not exist.

- [x] **Step 3: Implement identity resolution and validation**

  Use injected ID factories for deterministic tests and UUID-backed defaults in production. Resolve explicit candidate IDs first, then exact lowercase/trimmed fingerprints, never fuzzy-merge. Build initial contracts in two passes so temporary dependency references resolve only after stable IDs are assigned. Validate uniqueness, dependency existence, self-dependency, and DAG acyclicity before returning version 1.

- [x] **Step 4: Run identity tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: all schema and identity tests pass.

### Task 3: Versioned Atomic Delta Protocol

**Files:**
- Modify: `browser_use/recovery/manager.py`
- Modify: `tests/ci/recovery/test_contract_core.py`

**Interfaces:**
- Consumes: `SemanticContract`, `dict[str, UnitRuntimeState]`, and `ContractDelta`.
- Produces: `ContractManager.apply_delta(...) -> ContractApplyResult`, with `added_unit_ids` and `superseded_units` for the runtime-state layer.

- [x] **Step 1: Write failing Delta tests**

  Cover stale or mismatched Delta rejection, same-Delta temporary references, no-op Delta version stability, successful semantic change to a new immutable version, caller-visible atomicity after a later operation fails, constraint freezing after effects start, completed-definition freezing, supersede replacement and dependency rewiring, and rejection of superseding a unit whose effect is unknown.

- [x] **Step 2: Run Delta tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: Delta behavior assertions fail because `apply_delta` is absent.

- [x] **Step 3: Implement minimal atomic Delta application**

  Apply operations to local immutable copies; expose no mutation until every operation and final contract invariant validates. Increment the Contract version exactly once for a changed Delta, set `previous_version`, retain the old snapshot, and return the runtime-state handoff metadata. Treat a semantic no-op as the existing snapshot without a version increment.

- [x] **Step 4: Run Delta tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: all Contract and Delta tests pass.

### Task 4: Runtime-State Handoff and Minimal Scheduler

**Files:**
- Create: `browser_use/recovery/scheduler.py`
- Create: `browser_use/recovery/runtime_state.py`
- Modify: `browser_use/recovery/__init__.py`
- Modify: `tests/ci/recovery/test_contract_core.py`

**Interfaces:**
- Consumes: immutable Contract snapshots, runtime-state dictionaries, and `ContractApplyResult`.
- Produces: `RuntimeScheduler.ready_unit_ids`, `RuntimeScheduler.validate_activation`, `RuntimeStateManager.initialize`, `RuntimeStateManager.apply_contract_result`, and `RuntimeStateManager.activate`.

- [x] **Step 1: Write failing state and scheduling tests**

  Cover initial pending states, state creation for newly added units, superseded-state handoff, readiness derived only from pending state plus completed dependencies, skipped dependencies not satisfying edges, missing state coverage rejection, single-active-unit enforcement, and activation leaving the Contract version unchanged.

- [x] **Step 2: Run state/scheduler tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: imports or assertions fail because the state manager and scheduler do not exist.

- [x] **Step 3: Implement state handoff and scheduling**

  Keep `READY` and `BLOCKED` derived rather than persisted. Copy state dictionaries on each transition, initialize new units as pending, mark superseded units without rewriting the Contract, and permit at most one active semantic unit.

- [x] **Step 4: Run state/scheduler tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: all focused tests pass.

### Task 5: Quality and Regression Verification

**Files:**
- Modify as required by tools: `browser_use/recovery/*.py`, `tests/ci/recovery/test_contract_core.py`

**Interfaces:**
- Consumes: the completed Contract Core package.
- Produces: formatted, type-safe code with fresh verification evidence.

- [x] **Step 1: Format and lint the changed scope**

  Run: `uv run ruff format browser_use/recovery tests/ci/recovery`

  Run: `uv run ruff check browser_use/recovery tests/ci/recovery`

  Expected: Ruff reports no remaining errors.

- [x] **Step 2: Run focused tests from a fresh process**

  Run: `uv run pytest tests/ci/recovery/test_contract_core.py -q`

  Expected: all focused tests pass with zero failures.

- [x] **Step 3: Run static type checking on the changed scope**

  Run: `uv run pyright browser_use/recovery tests/ci/recovery/test_contract_core.py`

  Expected: zero type errors.

- [x] **Step 4: Run pre-commit on the changed files**

  Run: `uv run pre-commit run --files browser_use/recovery/__init__.py browser_use/recovery/contracts.py browser_use/recovery/manager.py browser_use/recovery/runtime_state.py browser_use/recovery/scheduler.py tests/ci/recovery/test_contract_core.py`

  Expected: all configured hooks pass.

- [x] **Step 5: Review the final diff against the Day 5 scope**

  Confirm there are no changes to Browser Use core execution, no persistence/checkpoint implementation, no fuzzy identity matching, and no edits to the user's `recoverable_runtime_MVP_docs` files.
