# Recoverable Runtime Architecture

This document describes the recovery semantics implemented under `browser_use/recovery/`. It focuses on the failure model and invariants rather than the Browser Use agent internals.

## 1. Problem model

A long-horizon web agent can fail after it has already caused an external side effect but before the local runtime has recorded a reliable outcome.

Typical examples include:

- submitting an application;
- sending a message;
- creating an order;
- updating a remote profile.

The dangerous case is not simply "the agent crashed." It is:

```text
external effect may have happened
        +
local runtime does not know whether it happened
        =
blind retry can duplicate the effect
```

This runtime therefore treats uncertain side effects as a first-class recovery state instead of assuming that a restart is safe.

## 2. Semantic task layer

Browser steps and model plans are too volatile to act as durable recovery identities. The runtime introduces a versioned `SemanticContract` containing stable `SemanticUnit` definitions.

A unit carries:

- a stable identity and business target;
- preconditions and postconditions;
- side-effect semantics;
- idempotency and reversibility metadata;
- a verification procedure;
- dependency information.

The runtime state of a unit is stored separately from the immutable contract definition. This allows execution state to change frequently without rewriting the semantic meaning of the task.

## 3. Runtime state

The main unit lifecycle is represented by `UnitStatus`:

```text
PENDING
  -> ACTIVE
  -> COMPLETION_CANDIDATE / VERIFYING
  -> COMPLETED

Failure and recovery paths may also use:
SKIPPED / SUPERSEDED / FAILED / UNKNOWN
```

Side-effect state is tracked separately through `EffectStatus`:

```text
NOT_APPLICABLE
NOT_STARTED
ATTEMPTED
COMMITTED
NOT_APPLIED
UNKNOWN
```

Verification is also independent:

```text
NOT_CHECKED
CHECKING
VERIFIED
REJECTED
INCONCLUSIVE
```

Keeping these dimensions separate is important: "the unit is active," "an external action was attempted," and "the postcondition is verified" are not equivalent facts.

## 4. Durable side-effect boundary

`SideEffectCoordinator` implements the durable boundary around an external action.

For one attempt:

```text
ACTIVE + executable effect
        |
        v
append PREPARED
persist checkpoint
        |
        v
execute external action
        |
        v
append ATTEMPTED
        |
        v
read-only verification
        |
        +---- VERIFIED ------> COMMITTED
        |
        +---- REJECTED ------> NOT_APPLIED
        |
        +---- INCONCLUSIVE --> UNKNOWN
```

The ordering is deliberate:

1. `PREPARED` is durable before the external executor is allowed to run.
2. `ATTEMPTED` records that execution crossed the external boundary.
3. Verification determines the authoritative observed outcome.
4. The closing ledger record and checkpoint are persisted together.

## 5. Checkpoint and Effect Ledger

Durable state is stored through the recovery persistence layer backed by SQLite.

A checkpoint records the latest recoverable runtime snapshot, including:

- workflow/run identity;
- contract version/snapshot;
- unit runtime states;
- the active unit;
- the last observed effect-ledger sequence.

The Effect Ledger is append-only at the semantic level. New records describe what the runtime learned about an attempt instead of erasing prior evidence.

This matters after a crash: a checkpoint may lag behind the ledger. Recovery reads the durable evidence after the checkpoint cursor and reconstructs the safe state instead of trusting the checkpoint alone.

## 6. Reconciliation

When recovery cannot prove whether an attempted side effect happened, the unit is kept `UNKNOWN`.

`ReconciliationCoordinator` then investigates the unresolved historical attempt through observation only:

```text
UNKNOWN attempt
      |
      v
find original effect_id / attempt_id
      |
      v
observe remote state
      |
      +---- VERIFIED ------> COMMITTED / completed
      |
      +---- REJECTED ------> NOT_APPLIED / retry may become legal
      |
      +---- INCONCLUSIVE --> remain UNKNOWN
```

Reconciliation does not create a new external attempt. It reuses the identity of the unresolved attempt and only records new evidence.

The central safety rule is:

> An inconclusive observation must never be treated as proof that the side effect did not happen.

This prevents the unsafe transition:

```text
UNKNOWN -> assume not applied -> retry -> duplicate side effect
```

## 7. Recovery safety gate

The runtime blocks a new side-effect execution unless the semantic state proves that execution is currently safe.

Conceptually:

```text
ACTIVE + NOT_STARTED  -> executable
ACTIVE + NOT_APPLIED  -> retryable

UNKNOWN               -> not executable
ATTEMPTED/uncertain   -> not executable
COMMITTED             -> not executable
```

The exact transition checks are implemented in the runtime-state and side-effect coordination modules and covered by recovery tests.

## 8. Integration with Browser Use

`RecoverableHarness` wraps the native Browser Use Agent loop rather than replacing the browser engine.

The integration adds:

- semantic context injection for the active unit;
- completion claims mapped back to semantic units;
- effect-boundary execution around side-effectful browser actions;
- durable checkpointing and resume;
- verification and reconciliation;
- Browser Use history persistence as supporting execution context.

The recovery implementation is concentrated in:

```text
browser_use/recovery/
```

with integration and fault-injection coverage under:

```text
tests/ci/recovery/
experiments/
```

## 9. v1 boundary

The general `RecoverableHarness` v1 formally supports browser-based verification.

The schema contains `EXTERNAL_TOOL` and `MIXED` verification-source values for future extension, but the v1 contract boundary rejects them. Lower-level experiments can inject a custom verifier, which is not the same as providing a general capability-sandboxed external-tool verification layer.

Other current limitations:

- semantic contracts are explicit runtime inputs rather than a universally reliable automatic task compiler;
- recovery safety depends on the quality of postconditions and observable evidence;
- the experiments cover controlled failure windows and are not a claim of exactly-once execution on arbitrary websites;
- the project focuses on side-effect safety and durable recovery, not on improving the underlying model's planning quality.

## 10. Upstream boundary

This repository is based on Browser Use v0.13.10. The retained upstream tree provides the real execution environment needed for end-to-end integration and experiments.

Upstream baseline used for the recovery work:

```text
5c892e013a73e6622e6f50336e1eb0aa2c4405f2
```

The recovery work is primarily under `browser_use/recovery/`, `tests/ci/recovery/`, and `experiments/`.
