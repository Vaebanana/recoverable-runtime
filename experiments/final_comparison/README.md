# Final Recovery Benchmark

This benchmark compares native Browser Use AgentHistory recovery with `RecoverableHarness` for a controlled non-idempotent application submit. Both arms use Browser Use 0.13.10, the same `ScriptedLLM`, browser settings, native `navigate → input → click` actions, process stops, per-step AgentHistory persistence, and safe history replay. The Harness arm adds the three-unit semantic contract, checkpoint, effect ledger, browser observation, and reconciliation.

Run a development pass from the repository root:

```powershell
uv run python -m experiments.final_comparison.runner --repeats 3
```

The default is 20 repeats per scenario and mode (240 trials). `--results-dir` selects the output directory. The runner writes `raw_trials.json`, `summary.json`, and `summary.md`. Each trial resets an independent SQLite-backed application server and starts a fresh worker. Crash workers terminate with `os._exit(91)`; recovery runs in a new process.

The six scenarios are normal completion, before effect, after effect before ATTEMPTED, after ATTEMPTED before history, after history commit, and a temporary verifier outage. The parent checks external `submit_count` and durable history/ledger at the stop point before recovery. S2 deliberately has an applied POST and only PREPARED in the Harness ledger.

Safe recovery requires submitted business status, exactly one POST, and no retry after an already-applied effect. The report gives each scenario first, then equal-weight aggregates. These controlled rates do not estimate production crash probabilities. Recovery action counts and duration are secondary metrics; browser startup makes latency noisy.
