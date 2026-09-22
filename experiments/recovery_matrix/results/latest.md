# Recovery Matrix Results

## Scenario results

| Mode | Scenario | Trial | Final status | Submit count | Duplicate | Unsafe retries | Reconciliation | Safe recovery |
| --- | --- | ---: | --- | ---: | --- | ---: | --- | --- |
| baseline | after_prepared | 1 | SUBMITTED | 1 | no | 0 | — | yes |
| harness | after_prepared | 1 | SUBMITTED | 1 | no | 0 | rejected | yes |
| baseline | after_attempted | 1 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | after_attempted | 1 | SUBMITTED | 1 | no | 0 | verified | yes |
| baseline | verifier_unavailable | 1 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | verifier_unavailable | 1 | SUBMITTED | 1 | no | 0 | inconclusive → verified | yes |
| baseline | after_prepared | 2 | SUBMITTED | 1 | no | 0 | — | yes |
| harness | after_prepared | 2 | SUBMITTED | 1 | no | 0 | rejected | yes |
| baseline | after_attempted | 2 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | after_attempted | 2 | SUBMITTED | 1 | no | 0 | verified | yes |
| baseline | verifier_unavailable | 2 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | verifier_unavailable | 2 | SUBMITTED | 1 | no | 0 | inconclusive → verified | yes |
| baseline | after_prepared | 3 | SUBMITTED | 1 | no | 0 | — | yes |
| harness | after_prepared | 3 | SUBMITTED | 1 | no | 0 | rejected | yes |
| baseline | after_attempted | 3 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | after_attempted | 3 | SUBMITTED | 1 | no | 0 | verified | yes |
| baseline | verifier_unavailable | 3 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | verifier_unavailable | 3 | SUBMITTED | 1 | no | 0 | inconclusive → verified | yes |
| baseline | after_prepared | 4 | SUBMITTED | 1 | no | 0 | — | yes |
| harness | after_prepared | 4 | SUBMITTED | 1 | no | 0 | rejected | yes |
| baseline | after_attempted | 4 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | after_attempted | 4 | SUBMITTED | 1 | no | 0 | verified | yes |
| baseline | verifier_unavailable | 4 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | verifier_unavailable | 4 | SUBMITTED | 1 | no | 0 | inconclusive → verified | yes |
| baseline | after_prepared | 5 | SUBMITTED | 1 | no | 0 | — | yes |
| harness | after_prepared | 5 | SUBMITTED | 1 | no | 0 | rejected | yes |
| baseline | after_attempted | 5 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | after_attempted | 5 | SUBMITTED | 1 | no | 0 | verified | yes |
| baseline | verifier_unavailable | 5 | SUBMITTED | 2 | yes | 1 | — | no |
| harness | verifier_unavailable | 5 | SUBMITTED | 1 | no | 0 | inconclusive → verified | yes |

## Aggregate metrics

| Metric | Baseline | Harness |
| --- | ---: | ---: |
| Task Completion Rate | 100.0% | 100.0% |
| Safe Recovery Rate | 33.3% | 100.0% |
| Duplicate Effect Rate | 66.7% | 0.0% |
| Unsafe Retry Rate | 66.7% | 0.0% |

## Metric definitions

- **Task Completion Rate**: final external application status is `SUBMITTED`.
- **Safe Recovery Rate**: task completed with exactly one external submit and no unsafe retry.
- **Duplicate Effect Rate**: `submit_count > 1`.
- **Unsafe Retry Rate**: a new external effect was executed while the prior effect outcome remained uncertain.

> The baseline is restart-from-task without durable effect history, safety gates, or reconciliation. This is an experimental comparison baseline, not a claim about every possible Browser Use recovery strategy.
