# Recovery Experiments

The experiments are designed to test a specific claim:

> After a crash near a non-idempotent external side effect, durable semantic effect history plus verification/reconciliation can prevent unsafe replay.

The experiments do not attempt to prove universal browser-agent reliability.

## 1. Recovery matrix

Source:

- `experiments/recovery_matrix/`
- `experiments/recovery_matrix/results/latest.md`

The matrix compares a restart-from-task baseline with the recoverable harness under three controlled failure conditions:

1. `after_prepared`: crash after execution intent is durably recorded but before the external action.
2. `after_attempted`: crash after the external action has been attempted.
3. `verifier_unavailable`: the external effect may have happened, but the first verification attempt is inconclusive.

Each mode/scenario pair is repeated five times in the committed report, producing 15 baseline trials and 15 harness trials.

### Aggregate result

| Metric | Restart baseline | Recoverable harness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

Metric definitions used by the experiment:

- **Task Completion Rate**: the final external application state is `SUBMITTED`.
- **Safe Recovery Rate**: the task completes with exactly one external submit and no unsafe retry.
- **Duplicate Effect Rate**: the external submit count is greater than one.
- **Unsafe Retry Rate**: a new external effect is executed while the previous outcome remains uncertain.

### Important baseline caveat

The table above uses the deliberately simple restart-from-task baseline implemented in `experiments/recovery_matrix/baseline.py`. It does **not** claim to represent every Browser Use recovery strategy.

A separate native `AgentHistory` baseline implementation lives in:

```text
experiments/native_history_baseline/
```

It exists to evaluate how far native Browser Use history/replay can recover before adding semantic effect-state machinery.

## 2. Process-level crash recovery

Source:

- `experiments/process_recovery/`
- `experiments/process_recovery/results/day10_summary.md`

This experiment uses separate worker processes and hard exits to ensure recovery is not merely an in-process exception path.

Committed result:

| Scenario | Crash ledger | Final unit | Final effect | Verification | Submit count | Result |
| --- | --- | --- | --- | --- | ---: | --- |
| after_prepared | prepared | completed | committed | verified | 1 | PASS |
| after_attempted | prepared -> attempted | completed | committed | verified | 1 | PASS |
| verifier_unavailable | prepared -> attempted | completed | committed | verified | 1 | PASS |

The `verifier_unavailable` scenario intentionally remains unresolved after the first recovery observation, then succeeds after later evidence becomes available. The key property is that the runtime does not issue a second submit while the previous attempt remains uncertain.

## 3. Why task completion alone is insufficient

A naive benchmark can report both systems as successful because both eventually reach `SUBMITTED`.

That hides a serious correctness difference:

```text
System A:
submit -> crash -> submit again -> SUBMITTED

System B:
submit -> crash -> reconcile old attempt -> SUBMITTED
```

Both "complete" the task. Only the second preserves the one-effect safety property.

For this reason the experiments separately measure:

- final task completion;
- duplicate effects;
- unsafe retries;
- reconciliation outcomes.

## 4. Reproducing the deterministic matrix

Start the controllable local application server:

```bash
uv run python experiments/recovery_smoke/server.py --port 8765
```

In another terminal run:

```bash
uv run python -m experiments.recovery_matrix.runner --headless --trials 5
```

The report is written to:

```text
experiments/recovery_matrix/results/latest.json
experiments/recovery_matrix/results/latest.md
```

## 5. Reproducing process recovery

Run:

```bash
uv run python -m experiments.process_recovery.run_experiment
```

The experiment launches its own local server and worker processes, injects hard-crash windows, reads SQLite recovery state after restart, and writes:

```text
experiments/process_recovery/results/day10_results.json
experiments/process_recovery/results/day10_summary.md
```

## 6. Test coverage

The recovery regression suite is located under:

```text
tests/ci/recovery/
```

It covers the contract model, runtime transitions, SQLite persistence, side-effect coordination, verification, reconciliation, bootstrap/resume, browser integration, fault injection, process recovery, and the recovery matrix.

Run it with:

```bash
uv run pytest tests/ci/recovery -q
```

The committed experiment reports should be treated as reproducible snapshots, not as substitutes for rerunning the suite after code changes.
