# Final Recovery Benchmark Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Compare native AgentHistory recovery with RecoverableHarness under six controlled non-idempotent browser crash windows.

**Architecture:** A parent runner owns an independent SQLite-backed application server and launches separate Python workers for execution and recovery. Both arms use the same Browser Use Agent, ScriptedLLM decisions, native navigate/input/click actions, atomic per-step AgentHistory persistence, and safe replay. The Harness arm alone adds a semantic contract, effect ledger, checkpoint, and observational reconciliation.

**Tech Stack:** Python 3.12, Browser Use 0.13.10, Pydantic v2, SQLite, FastAPI, pytest, uv.

**Spec:** User-supplied final comparison design in this conversation, based on `experiments/native_history_baseline`.

## Global Constraints

- Preserve the Day 9 experiment as historical code.
- Never use the Day 9 custom `submit_application` action in the final benchmark.
- Use the same scripted Browser Actions and crash definition on both arms.
- Keep the external application's SQLite database independent from both workers.
- Aggregate scenario rates are controlled-case results, not production crash probabilities.

## Review Focus

- The external effect is present but ATTEMPTED is absent in S2: assert the exact ledger and world state before recovery.
- The effect is absent in S1: reconcile PREPARED before a single safe click retry.
- History is absent for S2/S3 and present for S4: assert persisted action names at the crash boundary.
- S5 observation outage: assert UNKNOWN survives and no extra POST occurs before observation returns.
- Recovery metrics: count native action execution separately from safe replay and check server submit counts independently.

## Tasks

### Task 1: Shared browser workflow and models

**Files:** `experiments/recovery_smoke/server.py`, `experiments/final_comparison/models.py`, `experiments/final_comparison/workflow.py`, `tests/ci/recovery/test_final_comparison.py`.

- [x] Write a failing test that the controlled page exposes the applicant name input and that the final workflow produces native `navigate`, `input`, `click` actions with Harness metadata only on the click.
- [x] Run the focused test and confirm the failure is the missing workflow behavior.
- [x] Add the input and Pydantic scenario/trial models; reuse `ScriptedLLM` and the three-unit contract pattern from Harness integration tests.
- [x] Run the focused test and Ruff/Pyright on the touched files.

### Task 2: Durable worker and six crash windows

**Files:** `experiments/final_comparison/worker.py`, `experiments/final_comparison/history.py`, `tests/ci/recovery/test_final_comparison.py`.

- [x] Write a failing process test for S2 that checks world count 1, only PREPARED in Harness ledger, and no click in AgentHistory; add S0/S1/S3/S4/S5 assertions as parametrized cases.
- [x] Run the focused test and confirm the worker is missing.
- [x] Implement atomic per-step history persistence for both arms, native safe replay, real process termination, and fault injection at the shared native action seams. Resume Harness with `RecoverableHarness.resume()` and reconcile before any click.
- [x] Run the process matrix and check each crash boundary and recovery outcome.

### Task 3: Runner, metrics, and reports

**Files:** `experiments/final_comparison/runner.py`, `experiments/final_comparison/report.py`, `experiments/final_comparison/README.md`, `tests/ci/recovery/test_final_comparison.py`.

- [x] Write failing tests for per-scenario rates, aggregate rates, recovery action costs, and the controlled-case caveat.
- [x] Run them to confirm the report functions are missing.
- [x] Implement the CLI with configurable repeats (default 20), raw JSON results, summary JSON, and per-scenario Markdown.
- [x] Run one complete six-scenario repeat locally, the recovery test suite, pre-commit, Ruff, and Pyright; inspect generated results and report any limits.
