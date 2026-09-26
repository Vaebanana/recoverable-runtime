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

The main evaluation is a **fair Native Browser Use AgentHistory vs RecoverableHarness comparison**, not the earlier restart-from-task lower bound.

Both arms use Browser Use 0.13.10, the same scripted task, browser settings, native `navigate -> input -> click -> done` action sequence, process-level crash injection, per-step AgentHistory persistence, safe history replay, and history-aware resume. The Harness arm adds only the recovery semantics: Semantic Contract, Runtime Checkpoint, Effect Ledger, Verification, Reconciliation, and the runtime safety gate.

The controlled task is a non-idempotent application submit. External `status` and `submit_count` are recorded by an independent SQLite-backed application server and used as ground truth.

### Final benchmark: 240 controlled trials

Source:

```text
experiments/final_comparison/
```

Committed result:

[experiments/final_comparison/results/summary.md](experiments/final_comparison/results/summary.md)

Six scenarios are tested, with 20 Native trials and 20 Harness trials per scenario:

- `normal`
- `before_effect`
- `after_effect_before_attempted`
- `after_attempted_before_history`
- `after_history_commit`
- `verifier_unavailable`

That gives:

```text
6 scenarios × 2 modes × 20 repeats = 240 trials
```

Aggregate result:

| Mode | Trials | Task Completion | Safe Recovery | Duplicate Effect | Unsafe Retry |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native AgentHistory | 120 | 100.0% | 50.0% | 50.0% | 50.0% |
| Recoverable Harness | 120 | 100.0% | 100.0% | 0.0% | 0.0% |

The controlled scenarios are equally weighted test cases, **not estimates of production crash probabilities**.

The most important result is not "Native Browser Use fails 50% of the time." The correct conclusion is:

> In the three controlled windows where the external effect can occur before AgentHistory durably captures that fact, Native history-aware resume replays the submit, while the Harness preserves side-effect uncertainty and reconciles before retrying.

In particular:

| Scenario | Native | Harness |
| --- | --- | --- |
| after_effect_before_attempted | 20/20 duplicate + unsafe retry | 20/20 safe recovery |
| after_attempted_before_history | 20/20 duplicate + unsafe retry | 20/20 safe recovery |
| verifier_unavailable | 20/20 duplicate + unsafe retry | 20/20 blocks uncertainty and recovers safely |

This identifies the reliability boundary precisely:

> **AgentHistory becomes durable at Browser Step finalization; the Harness moves the side-effect durability boundary inside the step, before and immediately after the external effect.**

### Recovery cost

Recovery overhead is a secondary metric because browser startup dominates wall-clock time.

| Mode | Mean recovery time | Mean re-executed actions |
| --- | ---: | ---: |
| Native | 4426.6 ms | 2.17 |
| Harness | 4879.1 ms | 1.67 |

The Harness adds semantic recovery work but avoids some unsafe re-execution.

### Earlier lower-bound experiment

The earlier `experiments/recovery_matrix/` benchmark compares the Harness with a simpler restart-from-task baseline that does not persist durable effect history. It remains useful as a lower bound, but it is **not the primary comparison**.

That earlier result was:

| Metric | Restart-from-task | Recoverable Harness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

Detailed methodology and experiment evolution: [docs/recoverable-runtime/experiments.md](docs/recoverable-runtime/experiments.md)

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
├── final_comparison/            # final Native AgentHistory vs Harness benchmark
├── recovery_matrix/             # early restart lower-bound matrix
├── process_recovery/            # hard process crash/restart
└── native_history_baseline/     # earlier native AgentHistory baseline
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
- `experiments/final_comparison/`
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

### Final Native AgentHistory vs Harness benchmark

```bash
uv run python -m experiments.final_comparison.runner
```

The default is 20 repeats per scenario and mode, producing 240 trials.

### Earlier deterministic recovery matrix

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