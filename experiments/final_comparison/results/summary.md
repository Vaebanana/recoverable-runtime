# Final Recovery Benchmark

Native AgentHistory vs RecoverableHarness on a non-idempotent browser submit.

Crash scenarios are equally weighted controlled cases, not estimates of production crash probabilities.

| Scenario | Mode | Trials | Completion % | Safe recovery % | Duplicate % | Unsafe retry % |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| normal | native | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| normal | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| before_effect | native | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| before_effect | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| after_effect_before_attempted | native | 20 | 100.0 | 0.0 | 100.0 | 100.0 |
| after_effect_before_attempted | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| after_attempted_before_history | native | 20 | 100.0 | 0.0 | 100.0 | 100.0 |
| after_attempted_before_history | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| after_history_commit | native | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| after_history_commit | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |
| verifier_unavailable | native | 20 | 100.0 | 0.0 | 100.0 | 100.0 |
| verifier_unavailable | harness | 20 | 100.0 | 100.0 | 0.0 | 0.0 |

## Aggregate

| Mode | Trials | Completion % | Safe recovery % | Duplicate % | Unsafe retry % |
| --- | ---: | ---: | ---: | ---: | ---: |
| native | 120 | 100.0 | 50.0 | 50.0 | 50.0 |
| harness | 120 | 100.0 | 100.0 | 0.0 | 0.0 |

## Recovery cost (secondary)

| Mode | Before crash | After resume | Replayed | Reexecuted | Recovery ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| native | 3.00 | 1.50 | 1.67 | 2.17 | 4426.6 |
| harness | 3.00 | 1.17 | 2.00 | 1.67 | 4879.1 |
