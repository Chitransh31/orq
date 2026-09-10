# Presentation experiment operations

Implementation only: no experiment was launched while this package was prepared.
Do not start correctness, warmups, performance jobs, or recovery runs without a later explicit instruction to run them.

## Persistent operation

The coordinator is zf01. Start from `/home/sbagade/orq`. The detached supervisor has its own session, disconnected stdin, persistent logs, and a heartbeat. It continues independently of the chat and assistant usage limits while the host and its processes remain operational. A host reboot, administrator kill, or session-management policy can still terminate processes. There is no automatic reboot service and no automatic restart.

Default registry: `results/tpch-run-registry/<run-id>/`. This is outside experiment output directories and is excluded by the existing results ignore rule. It contains:

- `spec.json`: exact command, working directory, coordinator and source hashes;
- `status.json`: supervisor/runner identities, heartbeat, state, exit code;
- `supervisor.log` and `runner.log`: persistent stdout/stderr;
- archived status/spec files on an explicit resume.

Experiment outputs additionally contain immutable `manifest.json` and `schedule.json`, incremental `progress.json`, `active_process.json`, raw per-sample artifacts, reports, and presentation exports. The lock prevents concurrent selectivity workloads sharing build/output state. Do not launch the legacy runner or modify/rebuild the checkout while a study is active.

## First action in a later assistant session

Read this runbook, then inspect existing runs before any launch:

```bash
python3 scripts/tpch_supervisor.py status
python3 scripts/tpch_supervisor.py status RUN_ID
```

This is read-only and does not contact peers or restart anything. A running job should be left running. `unknown` means inspect the coordinator/process/logs before acting. `stale` means no recorded live process was found; it does not establish successful completion. `completed` is written only after the runner succeeds and its saved report validates as complete. A live orphaned launcher blocks a duplicate start.

When done, read `report.md`, `results.json`, `presentation/slide_claims.md`, and the logs. Check completeness and qualifications before claiming a result. Never infer success merely from a missing PID or a complete-looking log tail.

Explicit recovery and graceful stop, only when requested:

```bash
python3 scripts/tpch_supervisor.py resume RUN_ID
python3 scripts/tpch_supervisor.py stop RUN_ID
```

Resume retains the saved command and rejects source changes; the runner also validates configuration, schedule, catalog and prior samples. Successful samples are skipped. Partial attempts are archived. Stop targets verified Linux process identities using pidfds, not arbitrary PID numbers, and leaves the runner to perform its owned-process cleanup. An unresponsive process is reported; the supervisor does not silently declare it stopped.

## Approved matrix

SF0.1, 16 workers/party, seed 20260818, selection seed 20260908. WAN: qualitative userspace relay, 6.5 ms/direction, 12 Gbit/s configured cap per directed link; existing batching, communication-thread and preprocessing settings retained.

| Query | Exact point IDs | Candidates |
|---|---|---:|
| Q1 | q1-p10 | 2 |
| Q3 | q3-b21500-of600572, q3-b321757-of600572 | 3 |
| Q5 | q5-b1258-of150000, q5-b76251-of150000, q5-b150000-of150000 | 3 |
| Q8 | q8-b845-of20000, q8-b7714-of20000, q8-b20000-of20000 | 6 |
| Q9 | q9-b1771-of20000, q9-b6514-of20000, q9-b20000-of20000 | 6 |

53 combinations, 159 measured WAN executions at three repetitions, and 20 warmups for an unsplit run. Splitting into batches adds warmups for reused targets: include that extra cost in the budget. 100% retains all rows in their original order; the existing C++ selector returns sorted original indices, and correctness artifacts audit the complete index set. Base-input and shared-input digests use different serialization scopes and must not be compared directly.

This is a fixed public input-size study, not a private predicate/compaction benchmark. Every applicable target is run at every selected point. Q5/Q8/Q9 full-input anchors have no inferred DuckDB source-winner assignment: their purpose is comparing candidates on the original input size.

## Future launch sequence — examples, NOT executed

These commands are instructions for later authorized execution. First freeze the final source. Any source/tool edit invalidates old correctness authorization. Retain old artifacts; generate fresh matching evidence.

1. SF0.1 plaintext correctness for all 53 combinations:

```bash
python3 scripts/tpch_supervisor.py start presentation-correctness -- \
  --grid presentation --phase correctness --modes same \
  --scale-factor 0.1 --threads 16 --repetitions 1 --no-warmups \
  --output-dir results/tpch-orq-selectivity/presentation-correctness
```

Review this result before performance. Refresh the existing SF0.01 cross-mode smoke with the final source if executable changes require it: separate `--grid smoke --phase correctness --modes same,lan --scale-factor 0.01 --repetitions 1 --no-warmups` run, then compare validated input/output identities. This is an experiment, not an implementation test. Verify nonempty fixtures for each query family; preserve valid empty outputs rather than silently replacing them.

2. Prioritized benchmark batch: Q1 and full-input anchors. Reuse their three repetitions in final analysis.

```bash
python3 scripts/tpch_supervisor.py start presentation-anchors -- \
  --grid presentation --phase benchmark --modes wan \
  --points q1-p10,q5-b150000-of150000,q8-b20000-of20000,q9-b20000-of20000 \
  --scale-factor 0.1 --threads 16 --repetitions 3 \
  --correctness-dir results/tpch-orq-selectivity/presentation-correctness \
  --output-dir results/tpch-orq-selectivity/presentation-anchors
```

After anchors finish, estimate remaining costs from receipts, allowing for different input sizes/new plan behavior and extra batch warmups. The historical planning scenario is about 17 hours of timed work for the whole matrix, not a bound. Target 30 hours total with six hours contingency. A later session can make this read-only budget assessment; no assistant must remain active while a batch runs.

3. Remaining points, only after reviewing that budget:

```bash
python3 scripts/tpch_supervisor.py start presentation-reduced -- \
  --grid presentation --phase benchmark --modes wan \
  --points q3-b21500-of600572,q3-b321757-of600572,q5-b1258-of150000,q5-b76251-of150000,q8-b845-of20000,q8-b7714-of20000,q9-b1771-of20000,q9-b6514-of20000 \
  --scale-factor 0.1 --threads 16 --repetitions 3 \
  --correctness-dir results/tpch-orq-selectivity/presentation-correctness \
  --output-dir results/tpch-orq-selectivity/presentation-reduced
```

Budget fallback: omit `q8-b845-of20000` and `q9-b1771-of20000` before starting that batch. The resulting study has 123 timed executions. Document the omissions; do not select omissions based on observed wins/losses. There is no automatic launch of the next batch.

For an uncertain point, use a new run/output with its exact `--points`, `--block-start 3 --repetitions 3`, then optionally `--block-start 6 --repetitions 3`. Every candidate remains included. Original three-block outputs remain unchanged. The upper limit is nine blocks. If the budget expires, describe unresolved variation rather than claiming significance.

## Exports and slides

CSV/JSON/claim exports are generated automatically at runner completion or failure when sufficient artifacts exist. Figures are a separate read-only analysis of saved results, not a query run:

```bash
python3 scripts/export_tpch_presentation.py \
  results/tpch-orq-selectivity/presentation-anchors \
  results/tpch-orq-selectivity/presentation-reduced \
  --output-dir outputs/orq-presentation-study
.venv-duckdb-experiment/bin/python scripts/plot_orq_presentation.py outputs/orq-presentation-study
```

Add extra-block directories to the exporter only once. It rejects duplicate blocks, unpaired candidate coverage, changed input/binary identities and incompatible provenance. It preserves the initial results separately. Complete pairs in a partial study may appear in plots; omitted/failed pairs never enter winner comparisons. Always state actual completed coverage.

Exports: `samples.csv`, `comparisons.csv`, `physical_work.csv`, `stages.csv`, `inputs.csv`, `repeat_recommendations.csv`, `slide_claims.md`, and `plot_data.json`. Charts: full-input runtime, Q8/Q9 input-size sensitivity, Q8 38.570% runtime/bytes, and plan diagrams with physical join output lengths. SVG/PDF/PNG are written to `figures/`. The plotter requires matplotlib, available in the existing `.venv-duckdb-experiment` environment.

Run only these local implementation checks during development:

```bash
python3 -m unittest discover -s scripts/testing -p test_tpch_selectivity.py
python3 -m unittest discover -s scripts/testing -p test_tpch_presentation.py
```

Tests use parsed fixtures, mocked launches and harmless dummy Python processes. Do not run the general shell test harness: it can launch MPC binaries.
