# Recoverable Runtime for Long-Horizon Web Agents

[English](README.md) | [简体中文](README.zh-CN.md)

A fault-tolerant execution layer built on **Browser Use v0.13.10** for side-effectful long-horizon web-agent tasks.

The project focuses on one failure mode that ordinary task restart cannot safely solve:

```text
the agent may have already changed the external world
                    +
the local process crashes before it knows the outcome
                    =
blind replay can duplicate the side effect
```

Examples include submitting an application twice, creating duplicate orders, sending the same message again, or repeating a profile update.

This repository extends Browser Use with a semantic recovery runtime that persists effect intent, checkpoints runtime state, verifies postconditions, and reconciles uncertain historical attempts before allowing retries.

---

## What this project adds

```text
Browser Use Agent
       |
       v
Semantic Task Contract
       |
       v
Recoverable Harness
       |
       +--> Runtime State
       |
       +--> Effect Ledger --------+
       |                          |
       +--> Checkpoint            |
       |                          v
       +--> Verification --> Reconciliation
                                  |
                                  v
                            Safe Resume / Retry
```

Core capabilities:

- **Semantic Task Contract** — introduces stable semantic units above volatile browser steps and model plans.
- **Durable Effect Ledger** — records `PREPARED -> ATTEMPTED -> COMMITTED / NOT_APPLIED / UNKNOWN` evidence for external side effects.
- **Checkpoint + SQLite persistence** — persists unit runtime state and an effect-ledger cursor for crash recovery.
- **Verification** — uses read-only observation to prove postconditions instead of trusting a model completion claim.
- **Reconciliation** — investigates an unresolved historical attempt without creating a new external attempt.
- **Recovery safety gate** — blocks replay while a previous effect outcome remains uncertain.
- **Fault injection** — tests crashes at durable boundaries rather than only ordinary Python exceptions.
- **Browser Use integration** — wraps the native Agent loop instead of replacing the browser execution engine.

Detailed design: [docs/recoverable-runtime/architecture.md](docs/recoverable-runtime/architecture.md)

---

## Failure semantics

The durable side-effect path is:

```text
ACTIVE
  |
  v
PREPARED   <- execution intent is durable before the external action
  |
  v
execute external action
  |
  v
ATTEMPTED  <- the runtime knows the external boundary was crossed
  |
  v
verify postcondition
  |
  +---- VERIFIED ------> COMMITTED
  |
  +---- REJECTED ------> NOT_APPLIED
  |
  +---- INCONCLUSIVE --> UNKNOWN
```

The critical rule is:

> **UNKNOWN is not retryable.**

If the runtime cannot determine whether the side effect happened, it does not assume failure. It reconciles the original attempt first. Only evidence that the effect was not applied can make a retry legal.

This prevents:

```text
crash after submit
-> outcome unknown
-> restart task
-> submit again
-> duplicate external effect
```

---

## Recovery experiments

The repository contains controlled crash experiments rather than relying only on happy-path unit tests.

### Recovery matrix

The committed matrix repeats three fault scenarios across a restart baseline and the recoverable harness:

- crash after `PREPARED`;
- crash after `ATTEMPTED`;
- verifier temporarily unavailable.

Committed report: [experiments/recovery_matrix/results/latest.md](experiments/recovery_matrix/results/latest.md)

| Metric | Restart baseline | Recoverable harness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

The baseline in this table is deliberately **restart-from-task without durable effect history**. It is not presented as a claim about every Browser Use recovery strategy.

A separate native Browser Use `AgentHistory` baseline is implemented under:

```text
experiments/native_history_baseline/
```

to evaluate history/replay independently of the semantic effect-state machinery.

### Process-level crash recovery

The process-recovery experiment uses separate worker processes and hard exits, then reconstructs the workflow from SQLite state.

Committed report: [experiments/process_recovery/results/day10_summary.md](experiments/process_recovery/results/day10_summary.md)

| Scenario | Final unit | Final effect | Verification | External submit count |
| --- | --- | --- | --- | ---: |
| after_prepared | completed | committed | verified | 1 |
| after_attempted | completed | committed | verified | 1 |
| verifier_unavailable | completed | committed | verified | 1 |

Experiment methodology and metric definitions: [docs/recoverable-runtime/experiments.md](docs/recoverable-runtime/experiments.md)

---

## Repository map

The upstream Browser Use source tree is intentionally retained so the recovery layer can be tested against the real Agent and browser stack.

The main project-specific areas are:

```text
browser_use/recovery/
├── contracts.py                 # semantic contract and runtime schemas
├── runtime_state.py             # unit/effect/verification transitions
├── harness.py                   # Browser Use integration and resume
├── side_effects.py              # durable external-effect boundary
├── verification.py              # structured verification
├── observational_verifier.py    # read-only browser observation
├── reconciliation.py            # resolve UNKNOWN historical attempts
├── bootstrap.py                 # restore durable runtime state
├── effect_boundary.py           # map browser actions to effect execution
└── persistence/
    ├── storage.py               # SQLite runtime storage
    ├── checkpoint.py
    ├── effect_ledger.py
    └── models.py

tests/ci/recovery/               # recovery regression coverage

experiments/
├── recovery_smoke/              # controllable local external world
├── recovery_matrix/             # baseline-vs-harness fault matrix
├── process_recovery/            # hard process crash/restart
└── native_history_baseline/     # native AgentHistory comparison
```

---

## Upstream boundary and contribution scope

This is a **secondary development / systems extension of Browser Use**, not a from-scratch browser agent.

The recovery work was developed on top of Browser Use v0.13.10. The upstream baseline used to separate the recovery changes is:

```text
5c892e013a73e6622e6f50336e1eb0aa2c4405f2
```

The current recovery work is primarily concentrated in:

- `browser_use/recovery/`
- `tests/ci/recovery/`
- `experiments/recovery_smoke/`
- `experiments/recovery_matrix/`
- `experiments/process_recovery/`
- `experiments/native_history_baseline/`

To inspect the recovery implementation separately from the upstream baseline:

```bash
git diff 5c892e013a73e6622e6f50336e1eb0aa2c4405f2..main -- browser_use/recovery
```

Upstream project: [browser-use/browser-use](https://github.com/browser-use/browser-use)

---

## Reproduce the experiments

Requirements follow the upstream Browser Use development environment (Python 3.11+ and `uv`).

### Recovery regression suite

```bash
uv run pytest tests/ci/recovery -q
```

### Deterministic recovery matrix

Terminal 1:

```bash
uv run python experiments/recovery_smoke/server.py --port 8765
```

Terminal 2:

```bash
uv run python -m experiments.recovery_matrix.runner --headless --trials 5
```

### Process-level hard-crash recovery

```bash
uv run python -m experiments.process_recovery.run_experiment
```

The experiment launches its own controllable application server and worker processes.

---

## Design choices

### Why not use Browser Use step numbers as checkpoints?

Browser steps are execution-level artifacts. The same business intent may take different numbers of steps after replanning, page changes, or retries. Recovery therefore tracks stable semantic units rather than `step_n`.

### Why keep an Effect Ledger in addition to a checkpoint?

A checkpoint is a runtime snapshot; the ledger is ordered evidence of external-effect attempts. A crash can happen after a new ledger record but before the next checkpoint, so recovery must be able to replay durable ledger evidence after the checkpoint cursor.

### Why is verification separate from execution?

An executor returning normally does not prove that the desired external state exists, and a transport error does not prove that it does not exist. Verification is therefore modeled as an observational operation with `VERIFIED`, `REJECTED`, and `INCONCLUSIVE` outcomes.

### Why does reconciliation reuse the old attempt?

Reconciliation answers "what happened to this historical attempt?" It must not silently become another execution attempt. Reusing the original `effect_id` / `attempt_id` preserves that distinction.

---

## Current scope and limitations

The current implementation is intentionally narrower than a universal agent transaction system:

- the general `RecoverableHarness` v1 formally supports **browser-based verification**;
- `EXTERNAL_TOOL` / `MIXED` exist in the schema but are outside the v1 generic harness boundary;
- lower-level experiments can inject custom verifiers, but that is not yet a general capability-sandboxed external-tool observation layer;
- semantic contracts are explicit inputs rather than a fully automatic and universally reliable task compiler;
- the system provides recovery safety properties for the modeled effect boundary, not true distributed exactly-once semantics for arbitrary websites;
- it does not attempt to improve the underlying LLM's planning or browser-control accuracy.

These limits are explicit because the project is intended to make recovery behavior auditable rather than hide uncertainty behind task-level success.

---

## License

The retained Browser Use code is distributed under its original MIT License and copyright notice. See [LICENSE](LICENSE).

Recovery-specific additions in this repository are provided under the same repository license.