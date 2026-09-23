# Day 10 Process Recovery Experiment

- Generated at: `2026-09-22T15:44:26.862848+00:00`
- Overall: `PASS`

| Scenario | Crash ledger | Checkpoint seq | Ledger seq | Unit | Effect | Verification | Submit count | Result |
| --- | --- | ---: | ---: | --- | --- | --- | ---: | --- |
| after_prepared | prepared | 1 | 1 | completed | committed | verified | 1 | PASS |
| after_attempted | prepared → attempted | 1 | 2 | completed | committed | verified | 1 | PASS |
| verifier_unavailable | prepared → attempted | 1 | 2 | completed | committed | verified | 1 | PASS |
