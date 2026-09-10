# Fixed ORQ join associations under public input reduction

The source catalog is `manifest.json`. It locks DuckDB optimized/materialized
association evidence and the original frozen canonical manifest by SHA256. The
10-character `association_plan_id` is DuckDB study's SHA1 prefix, not SHA256.
No forced-order controls are used to choose candidates.

The artifact audit corrected two details in the task brief: Q8's existing
canonical association is `9d0bde819f` (the low-selectivity tree), and refinement
finds an additional association `350e47ddc5` at Part counts 824–865. Consequently
there are **ten new targets**, including `q8_selduckdb_d`, and 20 targets total.
The historical audit and PK/FK linearizations are in
`../../TPCH_SELECTIVITY_ORQ_IMPLEMENTATION_PLAN.md`.

## Targets and inputs

For every point run `qN` and `qN_duckdb`, plus:

| Query | Added targets | Varied relation |
|---|---|---|
| Q1 | none (negative control) | lineitem |
| Q3 | `q3_selduckdb_a` | lineitem |
| Q5 | `q5_selduckdb_a` | orders |
| Q8 | `q8_selduckdb_a`, `_b`, `_c`, `_d` | part |
| Q9 | `q9_selduckdb_a`, `_b`, `_c`, `_d` | part |

Wrappers select compile-time branches in the shared query files. CMake discovers
these `q*.cpp` files and provides aggregate target `tpch-selectivity-queries`.
The catalog records each target's exact tree, source fingerprints, join edges,
and unique keys. [The association reference](ASSOCIATIONS.md) lists each
validated PK-left/FK-right invocation sequence. The runtime trace checks the association actually executed;
the reporter also checks connected lineage, join predicates, input schemas,
and propagation of uniqueness to each left input. Q5 original has its existing
multi-branch descriptor rather than a single DuckDB tree.

Reduction is **public and before sharing**, and requires an explicit data seed.
The generator completes its original draws before gathering retained rows, so
nonvaried tables and FK domains remain unchanged. Rows rank lexically by
`md5('<rank-seed>:<pk>[:<pk>...]')`, with numeric PK ties. Lineitem uses
(OrderKey, LineNumber). Retained rows stay in original order. Percentages use
exact rational arithmetic and half-up rounding: `k = round(percent*N_ORQ/100)`.
Decimal/rational input components must fit uint64. Zero is allowed by the helper;
the experiment grid uses positive lengths. Selection indices, full/retained
input hashes and logical result bytes are retained during correctness runs.

Binary flags use a single dash and separate values, for example:
`-tpch-seed 20260818 -tpch-selectivity part=169/40 -tpch-selectivity-seed 20260908`.
Shell runner options use two dashes. Data seeds never seed protocol randomness.

## Exact grid

Each count below denotes the exact percentage `100 * count / source_base`;
counts are converted against each generated ORQ relation's actual length.

| Query | Source base | Counts |
|---|---:|---|
| Q1 | — | exactly 10% |
| Q3 | 600572 | 21500, 42940, 42941, 321757 |
| Q5 | 150000 | 1258, 2501, 2502, 76251 |
| Q8 | 20000 | 194, 385, 386, 605, 823, 824, 845, 865, 866, 7714, 14562, 14563, 17282 |
| Q9 | 20000 | 20, 37, 38, 1771, 3503, 3504, 6514, 9524, 9525, 12003, 14480, 14481, 17241 |

This is 35 query-points, 182 point/target cells, or **1,638 measured launches**
for three modes and three repetitions, plus warmups. Small-scale boundary points
can round to identical inputs; they remain separate source points and must not
be interpreted as distinct ORQ regimes. Smoke uses Q1 10%, Q3 count21500,
Q5 count1258, Q8 count845, Q9 count1771: 20 launches per mode/repetition.

## Protocol

Run from the repository root. First inspect the commands:

```bash
scripts/run-tpch-selectivity-3pc.sh --grid smoke --modes lan --repetitions 1 --dry-run
```

Create correctness evidence on the same SF, seeds, grid and source code that
will be benchmarked. Correctness runs omit QUERY_PROFILE and require both SQLite
success and byte-identical canonical logical outputs across variants.

```bash
scripts/run-tpch-selectivity-3pc.sh --phase correctness --grid smoke \
  --modes same --scale-factor 0.01 --threads 16 --repetitions 1 \
  --output-dir results/tpch-orq-selectivity/correctness-smoke
scripts/run-tpch-selectivity-3pc.sh --phase benchmark --grid smoke \
  --modes lan --scale-factor 0.01 --threads 16 --repetitions 3 \
  --correctness-dir results/tpch-orq-selectivity/correctness-smoke \
  --output-dir results/tpch-orq-selectivity/lan-smoke
```

After smoke validation, repeat correctness with `--grid full --scale-factor 0.1`,
then benchmark with `--grid full --scale-factor 0.1 --modes same,lan,wan`, using
that full correctness directory. Defaults are hosts zf01/zf02/zf03, 16 threads,
three repetitions, data seed20260818, rank seed20260908. Userspace WAN uses the
existing distributed rootless relay; no sudo is needed. WAN explicitly supplies
`-l 6.5 -w 12` to ORQ’s arithmetic cost model as well as shaping traffic. Its application cap and
delay approximate WAN conditions; they do not claim kernel-level fidelity.

The runner randomizes complete repetition blocks deterministically, warms each
target/mode, permits only one owner of the shared build, checks source/input
identity throughout, retains failed attempts, and writes incomplete reports on
failure. Resume with the identical command plus `--resume`; changed source,
configuration or schedule is rejected. Different build modes are sequential.
Allow approximately 6–9 days for the full matrix based on historical runs;
replace this estimate with measured smoke throughput before scheduling it.
No full experimental performance result is implied by successful unit tests.

Regenerate a report with:

```bash
python3 scripts/report_tpch_selectivity.py --output-dir RESULTS_DIRECTORY
```

Inspect `--help` for exact reporter options if invoking outside these examples.
Reports include completeness, per-stage timings, per-party byte scopes,
intermediate table cardinalities, input counts, median/MAD/range/stdev,
per-target deltas to both baselines, best new target (including losses), and
DuckDB-choice regret. Bytes sum all parties within each repetition before
aggregation. Query/workload summaries use party 0 wall time, matching the
existing stopwatch convention; all party scopes remain in the raw samples.
Zero plaintext bytes never produce percentage savings. Noisy
comparisons (MAD/median >10%) are descriptive; rerun complete paired blocks
with six or nine repetitions in a separate output directory when needed.
The runner does not silently add adaptive repetitions to a frozen schedule.

## Interpretation and validation

Query time excludes generation/reduction/sharing; workload time includes input
setup through final query computation. Input setup includes the audit hashing
cost, which is reported separately with sharing and reduction measurements.
Output opening/SQLite checking is excluded from profiled performance runs.
Original ORQ predicates are retained, unlike the DuckDB controlled SQL study's
predicate replacement. Q3 deterministic tie handling and Q8 common projection
are shared across every experiment variant, including baselines.

DuckDB is a candidate generator. Its orientation/hash implementation and its
DBGEN thresholds are not transferred. ORQ's own fanout, oblivious sorting,
communication, and PK/FK primitive determine the winner. DuckDB materialized
query timing excludes CTAS+ANALYZE. Public pre-sharing reduction models naturally
small inputs; it does not measure a private filter or compaction pipeline.
Such a pipeline requires a separate experiment with its costs attributed.

```bash
bash -n scripts/run-tpch-selectivity-3pc.sh
.venv-duckdb-experiment/bin/python -m unittest discover -s scripts/testing -p 'test_*.py'
cmake --build build --target test_tpch_selectivity tpch-selectivity-queries -j 2
```

Run the fixture using the same protocol/runtime environment as the build. It
checks exact rounding, MD5 vectors, nested deterministic selection, malformed
inputs, duplicate PK rejection, gathering, and compound PK/FK joins with
repeated and missing foreign keys. New result directories must pass reporter
validation before any performance comparison is presented as complete.
