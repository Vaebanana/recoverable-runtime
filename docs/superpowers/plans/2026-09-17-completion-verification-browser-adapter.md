# Completion Verification and Browser Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Day 5 with an evidence-bearing completion-verification state machine and a sidecar adapter that plugs into Browser Use's existing step hooks without changing the Agent execution loop.

**Architecture:** Keep semantic definitions immutable in `SemanticContract` and all execution transitions in `RuntimeStateManager`. A generic observational `Verifier` produces structured evidence; `VerificationManager` controls the candidate/verification lifecycle. `BrowserUseRuntimeAdapter` owns one runtime snapshot, injects a typed semantic context at step start, captures a compact trace at step end, accepts only structured completion claims, and delegates verification/state changes to the managers.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest 9, Ruff, Pyright, uv

**Specs:** `recoverable_runtime_MVP_docs/Day5/CompletionVerification和接入点分析.md` and `recoverable_runtime_MVP_docs/Day5/Task5、6代码参考`

## Global Constraints

- Use `uv` for every Python command and Pydantic v2 for runtime schemas and adapter I/O.
- Only `VerificationOutcome.VERIFIED` may produce `UnitStatus.COMPLETED`.
- Verification is observational. A verifier must explicitly declare that property before it can run.
- `REJECTED` returns the unit to `ACTIVE`; `INCONCLUSIVE` moves it to `UNKNOWN` and blocks ordinary step progress.
- Expected identity comes from `SemanticUnit.target`; observed values come from `VerificationEvidence`. The Runtime does not infer completion from prose.
- The adapter accepts a structured `CompletionClaim` from an injected source; it does not parse the model's natural-language response.
- Use only `Agent.run(on_step_start=..., on_step_end=...)`. Do not modify `service.py`, the multi-action executor, or tool execution.
- Keep message-manager compatibility code isolated behind a semantic-context sink.
- Contract generation, Delta proposal generation, action-level policy gates, checkpoints, SQLite, effect ledgers, reconciliation, and recovery policy remain out of scope.

---

### Task 1: Verification Schemas and Runtime State Machine

**Files:**
- Create: `browser_use/recovery/verification.py`
- Modify: `browser_use/recovery/contracts.py`
- Modify: `browser_use/recovery/runtime_state.py`
- Modify: `browser_use/recovery/__init__.py`
- Create: `tests/ci/recovery/test_verification.py`

**Interfaces:**
- Produces: `VerificationOutcome`, `VerificationEvidence`, `VerificationResult`, `Verifier`, `VerificationManager`, and typed verification/state-transition errors.
- Extends: `EffectStatus.NOT_APPLICABLE`, `RuntimeStateManager.claim_completion`, `begin_verification`, and `apply_verification_result`.

- [x] **Step 1: Write failing verification and transition tests**

  Cover immutable structured evidence, read-only verifier enforcement, `ACTIVE -> COMPLETION_CANDIDATE -> VERIFYING`, all three outcomes, copy-on-write behavior, no-side-effect initialization, illegal transitions, verifier exceptions becoming `INCONCLUSIVE`, and cancellation propagation.

- [x] **Step 2: Run the focused tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_verification.py -q`

  Expected: collection fails because verification interfaces do not exist.

- [x] **Step 3: Implement schemas and state transitions**

  Add frozen evidence/result models with immutable expected/observed mappings. Require exact contract/state coverage and the correct source status for every transition. Map outcomes to state/effect pairs exactly as specified. Preserve generic `UnitRuntimeState` defaults while initializing read-only units as `NOT_APPLICABLE` through the manager.

- [x] **Step 4: Implement `VerificationManager` orchestration**

  Reject verifiers that do not declare `observational=True`. Transition into `VERIFYING` before awaiting the verifier. Convert ordinary verifier failures into an evidence-bearing `INCONCLUSIVE` result, but propagate task cancellation. Apply the returned result through `RuntimeStateManager`.

- [x] **Step 5: Run the focused tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_verification.py -q`

  Expected: all verification tests pass.

### Task 2: Typed Semantic Context

**Files:**
- Create: `browser_use/recovery/semantic_context.py`
- Modify: `browser_use/recovery/__init__.py`
- Create: `tests/ci/recovery/test_semantic_context.py`

**Interfaces:**
- Produces: `SemanticUnitContext`, `SemanticRuntimeContext`, `SemanticContextBuilder`, and deterministic prompt rendering.

- [x] **Step 1: Write failing context tests**

  Cover active-unit identity, postconditions, verification procedure/source, effect warnings, ready-unit IDs, deterministic rendering, exact state coverage, multiple-active rejection, and UNKNOWN blocking metadata.

- [x] **Step 2: Run context tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_semantic_context.py -q`

  Expected: collection fails because semantic-context interfaces do not exist.

- [x] **Step 3: Implement the context builder and renderer**

  Derive readiness through `RuntimeScheduler`, never persist READY. Surface a single active unit when present. If any unit is UNKNOWN, set `progress_blocked=True`, include the affected IDs and a recovery-oriented reason, and do not select a new unit.

- [x] **Step 4: Run context tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_semantic_context.py -q`

  Expected: all context tests pass.

### Task 3: Browser Use Step-Hook Adapter

**Files:**
- Create: `browser_use/recovery/browser_use_adapter.py`
- Modify: `browser_use/recovery/__init__.py`
- Create: `tests/ci/recovery/test_browser_use_adapter.py`

**Interfaces:**
- Produces: `CompletionClaim`, `CompletionClaimSource`, `SemanticContextSink`, `BrowserUseMessageContextSink`, `BrowserUseStepRecord`, `BrowserUseRuntimeAdapter`, and typed adapter errors.
- Consumes: the actual Browser Use `Agent` hook shape, `Agent.state.last_model_output`, `Agent.state.last_result`, and `agent.message_manager`.

- [x] **Step 1: Write failing adapter tests with small typed fakes**

  Cover direct hook compatibility, start-context publication, exact-one-ready auto-activation, no arbitrary activation when several units are ready, UNKNOWN pre-step blocking, compact trace capture, no claim/no transition, structured claim verification, wrong-unit claim rejection, and the default message sink's isolated compatibility behavior.

- [x] **Step 2: Run adapter tests and verify RED**

  Run: `uv run pytest tests/ci/recovery/test_browser_use_adapter.py -q`

  Expected: collection fails because the adapter interfaces do not exist.

- [x] **Step 3: Implement start-hook behavior**

  Validate and snapshot semantic state, auto-activate only when exactly one unit is ready, build the context, raise a typed block before ordinary progress when UNKNOWN exists, and publish context through the injected sink. The default Browser Use sink must contain the sole private message-manager call and fail with a clear compatibility error if that API changes.

- [x] **Step 4: Implement end-hook behavior**

  Record only serializable step summaries. Ask the injected claim source for a structured claim; ignore absent or false claims; require the claimed unit to equal the active unit; then run completion claim plus verification and atomically replace the adapter's state snapshot. Do not inspect prose to infer completion or fabricate Contract Deltas.

- [x] **Step 5: Run adapter tests and verify GREEN**

  Run: `uv run pytest tests/ci/recovery/test_browser_use_adapter.py -q`

  Expected: all adapter tests pass.

### Task 4: Quality and Regression Verification

**Files:**
- Modify as required by tools: `browser_use/recovery/*.py`, `tests/ci/recovery/*.py`

- [x] **Step 1: Format and lint the changed scope**

  Run: `uv run ruff format browser_use/recovery tests/ci/recovery`

  Run: `uv run ruff check browser_use/recovery tests/ci/recovery`

- [x] **Step 2: Run all recovery tests from a fresh process**

  Run: `uv run pytest tests/ci/recovery -q`

- [x] **Step 3: Run static type checking on the changed scope**

  Run: `uv run pyright browser_use/recovery tests/ci/recovery`

- [x] **Step 4: Run pre-commit on every changed implementation/test/plan file**

  Run the repository hooks with an explicit `--files` list.

- [x] **Step 5: Run the broader CI suite and classify unrelated baseline failures**

  Run: `$env:PYTHONUTF8='1'; uv run pytest tests/ci -q`

  If a known platform/baseline failure remains, rerun enough of the suite to show the new recovery tests are independent and report the exact pre-existing failure rather than hiding it.

- [x] **Step 6: Review the final diff against scope**

  Confirm `browser_use/agent/service.py` and action execution are unchanged, completion is never inferred from prose, only VERIFIED completes, UNKNOWN blocks normal progress, and no Day 6 persistence/recovery features were added.
