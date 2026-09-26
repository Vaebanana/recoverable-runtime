# Recovery Experiments

The experiments evaluate a specific claim:

> For a non-idempotent browser side effect, durable semantic effect state can make recovery safe across crash windows that are not fully represented by native AgentHistory.

The project does **not** claim universal exactly-once execution or estimate real-world browser crash probabilities.

## 1. Final comparison: Native AgentHistory vs RecoverableHarness

Source:

- `experiments/final_comparison/`
- `experiments/final_comparison/results/summary.md`
- `experiments/final_comparison/results/summary.json`

This is the project's primary comparison.

Both modes use:

- Browser Use 0.13.10;
- the same scripted non-idempotent submit task;
- the same browser settings;
- native `navigate -> input -> click -> done` actions;
- process-level crash injection;
- per-step AgentHistory persistence;
- safe history replay;
- history-aware resume;
- an independent SQLite-backed application server as external ground truth.

The Harness mode additionally uses:

- Semantic Contract;
- Runtime Checkpoint;
- Effect Ledger;
- Verification;
- Reconciliation;
- Recovery Safety Gate.

The controlled application exposes external `status` and `submit_count`. A run can therefore finish with `SUBMITTED` and still be marked unsafe if the submit happened twice.

## 2. Six controlled scenarios

Each scenario is run 20 times in Native mode and 20 times in Harness mode:

1. `normal` — no crash.
2. `before_effect` — crash before the external submit.
3. `after_effect_before_attempted` — the submit reached the external world, but Harness has only durable `PREPARED`; AgentHistory has not finalized the click.
4. `after_attempted_before_history` — Harness has durable `PREPARED -> ATTEMPTED`, while AgentHistory still lacks the finalized click.
5. `after_history_commit` — the submit is already present in durable AgentHistory.
6. `verifier_unavailable` — the side effect happened, but recovery evidence is temporarily unavailable.

Total:

```text
6 scenarios × 2 modes × 20 repeats = 240 trials
```

The scenarios are deliberately equally weighted controlled cases. Their aggregate percentages are **not production failure probabilities**.

## 3. Primary metrics

- **Task Completion Rate**: final external status is `SUBMITTED`.
- **Duplicate Effect Rate**: final external `submit_count > 1`.
- **Unsafe Retry Rate**: recovery executes another submit after an already-applied external effect.
- **Safe Recovery Rate**: task completes, exactly one submit exists, and no unsafe retry occurs.

The distinction matters because:

```text
submit
-> crash
-> submit again
-> final status = SUBMITTED
```

has 100% task completion but is not a correct recovery.

## 4. Final aggregate result

| Mode | Trials | Completion | Safe recovery | Duplicate | Unsafe retry |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native AgentHistory | 120 | 100.0% | 50.0% | 50.0% | 50.0% |
| RecoverableHarness | 120 | 100.0% | 100.0% | 0.0% | 0.0% |

The correct interpretation is not that Native Browser Use has a 50% real-world failure rate.

The controlled result is:

> Native AgentHistory is safe when the external effect either has not happened yet or has already been durably finalized in history. In the tested windows where the external effect occurs before history records that fact, history-aware resume cannot distinguish "effect happened but history is missing" from "effect never happened" and re-executes the submit.

The Harness stores side-effect evidence at a finer boundary and therefore has additional information available during recovery.

## 5. Critical crash windows

### after_effect_before_attempted

At the crash:

```text
External world: submit_count = 1
AgentHistory:    navigate -> input
Harness ledger: PREPARED
```

Native does not have durable history evidence for the click and submits again.

Result over 20 trials:

| Mode | Safe recovery | Duplicate | Unsafe retry |
| --- | ---: | ---: | ---: |
| Native | 0% | 100% | 100% |
| Harness | 100% | 0% | 0% |

The Harness does not interpret `PREPARED` as proof that the effect did not happen. It reconciles the external state before deciding whether a retry is legal.

### after_attempted_before_history

At the crash:

```text
External world: submit_count = 1
AgentHistory:    navigate -> input
Harness ledger: PREPARED -> ATTEMPTED
```

Again, Native lacks a finalized click in durable history, while the Harness knows that the external boundary was crossed.

Result over 20 trials:

| Mode | Safe recovery | Duplicate | Unsafe retry |
| --- | ---: | ---: | ---: |
| Native | 0% | 100% | 100% |
| Harness | 100% | 0% | 0% |

### verifier_unavailable

The effect already happened, but the first recovery observation is unavailable.

Harness behavior:

```text
Reconciliation
-> INCONCLUSIVE
-> UNKNOWN
-> runtime gate blocks submit
-> evidence becomes available
-> Reconciliation
-> VERIFIED
-> COMMITTED
```

The committed raw-trial-derived summary shows all 20 Harness trials recovered without another submit.

This validates a central safety rule:

> Unknown does not mean not applied.

## 6. Reliability boundary identified by the experiment

The experiment supports the following narrower conclusion:

> **AgentHistory's durable knowledge boundary is Browser Step finalization. RecoverableHarness moves side-effect durability inside the step by recording execution intent before the effect and execution evidence immediately around the effect boundary.**

This is the main technical difference demonstrated by the benchmark.

## 7. Recovery cost

Secondary aggregate metrics:

| Mode | Mean actions before crash | Mean actions after resume | Mean replayed | Mean re-executed | Mean recovery time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native | 3.00 | 1.50 | 1.67 | 2.17 | 4426.6 ms |
| Harness | 3.00 | 1.17 | 2.00 | 1.67 | 4879.1 ms |

Latency is secondary because browser/process startup contributes substantial noise. The result is mainly useful to show that safer recovery did not require a large increase in action re-execution.

## 8. Earlier experiments

### Restart-from-task lower bound

Source:

- `experiments/recovery_matrix/`
- `experiments/recovery_matrix/results/latest.md`

This earlier experiment compares RecoverableHarness with a simpler baseline that restarts from the task without durable effect history.

| Metric | Restart baseline | RecoverableHarness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

This result is retained as a lower bound and project-evolution artifact. It is **not** the primary Browser Use comparison.

### Earlier Native AgentHistory baseline

`experiments/native_history_baseline/` contains the earlier three-window AgentHistory experiment used while refining the final benchmark.

### Process-level recovery experiment

`experiments/process_recovery/` validates durable SQLite recovery across independent processes and hard exits.

These experiments remain useful regression and design evidence, but the six-scenario `final_comparison` benchmark supersedes them as the main comparative evaluation.

## 9. Reproduce the final benchmark

From the repository root:

```bash
uv run python -m experiments.final_comparison.runner
```

The default is 20 repeats per scenario and mode.

Outputs:

```text
experiments/final_comparison/results/raw_trials.json
experiments/final_comparison/results/summary.json
experiments/final_comparison/results/summary.md
```

The repository commits the summary snapshots while raw trial output remains a generated artifact.

For a shorter development pass:

```bash
uv run python -m experiments.final_comparison.runner --repeats 3
```

## 10. Test coverage

The final comparison has dedicated regression coverage in:

```text
tests/ci/recovery/test_final_comparison.py
```

The broader recovery suite is:

```bash
uv run pytest tests/ci/recovery -q
```

The committed reports are reproducible snapshots and should be regenerated after benchmark or recovery-semantic changes.
