# Experiment scripts

The `scripts/` directory aggregates helper scripts for setting up ORQ’s dependencies, running experiments, and visualizing results.

Sub-directories:
- `comm/` - communication scripts, including WAN simulation
- `competitors/` - scripts for running competitors in the ORQ paper
- `orchestration/` - cluster setup, AWS
- `plot/` - plotting scripts
- `profiling/` - profiling scripts
- `sosp25-replication` - directory for SOSP Artifact Evaluation
- `testing/` - test helpers

Top-level scripts:
- `setup.sh` – One-shot installer for ORQ’s external dependencies; calls:
    - `_setup_required.sh` – `apt install` required dependencies
    - `_setup_libote.sh` – fetches and builds libOTe
    - `_setup_securejoin.sh` – fetches and builds secureJoin
- `_update_hostfile.sh` – Updates the hostfile for multi-node runs.
- `run_experiment.py` – Generic wrapper that compiles and executes a program.
  Its `--wan-sim auto|off` option controls whether `-s wan` also manages the
  repository's Linux traffic shaper.
- `query-experiments.sh` – Helper for query benchmarks.
- `run-tpch-plain-and-3pc.sh` – Runs the original and/or fixed DuckDB-derived
  TPC-H Q1, Q3, Q5, Q8, and Q9 plans at SF 0.1 in plaintext 1PC and replicated
  secure 3PC LAN/WAN modes, preserving per-query profiles and producing JSON
  and Markdown reports.
- `report_tpch_plain_and_3pc.py` – Normalizes the focused runner's raw profiles
  and logs into `plans.json`, `results.json`, and `report.md`.
- `analyze_duckdb_tpch.py` – Generates standard TPC-H data in DuckDB and studies
  how predicate selectivity changes estimated/actual operator cardinalities and
  optimized join orders. It also executes a connected forced-order control and
  writes raw plans, CSV data, and a Markdown report under `results/`.
- `compare-to.py` – Compares two branches' performance.


Run the fork's studies using the sections below. Commands run from the repository
root unless a block explicitly changes directory. The generic runner is invoked
from `scripts/` or `build/`.

- [Focused ORQ TPC-H benchmark](#focused-orq-tpc-h-benchmark): original and canonical DuckDB-derived plans.
- [DuckDB selectivity experiment](#duckdb-selectivity-experiment): original-predicate sweeps.
- [Controlled DuckDB selectivity study](#controlled-duckdb-selectivity-study): direct and materialized inputs with fixed-order controls.
- [Rootless local WAN simulation](#rootless-local-3pc-wan-simulation-linux): Linux namespaces and netem.
- [Distributed WAN simulation without sudo](#distributed-wan-simulation-without-sudo): userspace relays.
- [ORQ input reduction study](#orq-join-associations-under-public-input-reduction): smoke and full grids.
- [Detached presentation study](#detached-presentation-study): smaller matrix, recovery, and exports.
- [Original SOSP experiments](sosp25-replication/README.md): historical artifact reproduction.

## Focused ORQ TPC-H benchmark

Install the dependencies described in the [main README](../README.md), deploy
ORQ on each Linux host, and put the checkout's `startmpc` launcher on `PATH`.
Configure SSH aliases for the three parties and use the same absolute checkout
path on each machine. The coordinator needs noninteractive SSH access to all
three aliases, including itself. For example, from the coordinator:

```bash
./scripts/orchestration/deploy.sh "$PWD" node0 node1 node2
export PATH="$PWD/include/backend/nocopy_communicator/startmpc:$PATH"
```

For DuckDB analysis and plotting, create a Python environment from the repository
root. ORQ experiment runners use `python3`, so activate this environment in the
coordinator shell if you need these dependencies there:

```bash
python3 -m venv .venv-experiments
source .venv-experiments/bin/activate
python3 -m pip install -r requirements.txt
```

Distributed measurements require Linux. macOS can inspect dry-run commands and
analyze saved artifacts. Use a fresh result directory for each study; generated
`results/` and `outputs/` directories are ignored by Git.

Run this benchmark from the repository root on `node0` of a Linux cluster:

```bash
./scripts/run-tpch-plain-and-3pc.sh
```

The default matrix is Q1, Q3, Q5, Q8, and Q9 at SF 0.1, with 16 worker
threads and three repetitions of each of these modes:

- plaintext 1PC with the `same` setting;
- replicated 3PC over LAN using NoCopy and four communication threads;
- replicated 3PC over simulated WAN using NoCopy and ORQ's WAN batching
  defaults.

1PC is intentionally not repeated with `lan` or `wan`: it has one process and
no inter-party network, and ORQ explicitly ignores those settings for 1PC.
The default `--plan-set original` remains the original 15 run groups (45
executions). To compare every original plan with its DuckDB-derived variant,
run 30 groups (90 executions):

```bash
./scripts/run-tpch-plain-and-3pc.sh --plan-set both
```

Both variants receive the same deterministic synthetic-data seed, `20260818`
by default. Override it with `--data-seed UINT64`. This seed controls generated
TPC-H table values only; it deliberately does not seed MPC protocol randomness.

Inspect the commands safely from macOS without building or connecting to any
servers:

```bash
./scripts/run-tpch-plain-and-3pc.sh --dry-run
./scripts/run-tpch-plain-and-3pc.sh --plan-set both --dry-run
```

macOS is supported for command inspection, the Python unit tests, manifest and
report generation, and whatever local C++ compilation the installed toolchain
allows. The distributed launcher, communicator measurements, and `tc/netem`
WAN acceptance runs target Linux and must be run from the Linux coordinator.

Override the measurement defaults or use an existing real WAN without changing
its network configuration:

```bash
./scripts/run-tpch-plain-and-3pc.sh \
  --threads 8 \
  --repetitions 5 \
  --node-prefix node

./scripts/run-tpch-plain-and-3pc.sh \
  --wan-mode real \
  --output-dir results/tpch-real-wan
```

The hosts must resolve as `<prefix>0`, `<prefix>1`, and `<prefix>2`.
`node0` must be able to SSH non-interactively to all three aliases, including
itself. The ORQ checkout and `build/` directory must have the same absolute path
on each host because the launcher executes and copies binaries at that path.
The script checks these requirements before compiling anything.

For simulated WAN, ORQ applies Linux `tc netem` at 12 Gbit/s with 6.5 ms delay
per link (approximately 13 ms RTT). This requires `tc` and passwordless `sudo`
on all three machines. If a run is forcibly terminated and shaping
remains enabled, remove it from `node0` with:

```bash
./scripts/comm/cluster-wan-sim.sh off node1 node2
```

`--wan-mode real` passes `-s wan` to ORQ but disables `tc` management. Use it
only when the three party hosts are genuinely connected over the intended WAN.
A VPN connection from a MacBook to the university does not turn traffic between
two same-campus servers into WAN traffic. Also, 3PC does not select a different
algorithm for `lan` and `wan`; the measured difference comes from the effective
server-to-server latency and bandwidth. The default simulated mode is therefore
the appropriate LAN-versus-WAN comparison when all three machines are in the
same university pool.

Results are written under a timestamped `results/tpch-plain-and-3pc/` directory:

```text
plaintext-1pc/same/             # per-query .log and .json files
secure-3pc/lan/                 # per-query .log and .json files
secure-3pc/wan-simulated/       # or wan-real
meta.txt                        # coordinator, CPU, network, and git metadata
plans.json                      # frozen DuckDB and handwritten ORQ plans
results.json                    # normalized machine-readable measurements
report.md                       # plans, timing tables, and artifact links
```

Original artifacts are named `qN.log` and `qN.json`; DuckDB-derived artifacts
are `qN-duckdb.log` and `qN-duckdb.json`. `results.json` keeps the plan variant,
per-stage timing statistics, observed physical operator traces, communication
bytes, RTT samples, failures, and environment metadata. Its paired comparison
uses `original mean / DuckDB-derived mean`, so a speedup above 1 means the
derived plan was faster. Differently named stages are reported separately.

ORQ does not have a DuckDB-style optimizer or `EXPLAIN`. Its TPC-H programs are
handwritten C++ physical plans. The report records those fixed operator/join
orders and combines them with `QUERY_PROFILE` stage runtimes, observed
join/sort/aggregate sizes from `INSTRUMENT_TABLES`, and communicator bytes. The
reported query runtime starts after synthetic database construction; profiling
mode also skips SQLite correctness validation and result opening.

The canonical DuckDB 1.5.4 SQL and raw `EXPLAIN (FORMAT JSON)` snapshots live
in `scripts/tpch-duckdb-plans/manifest.json` and its adjacent `.sql` and
`.explain.json` files. Q1 and Q3 are intentionally compiled no-op controls.
Q5, Q8, and Q9 are handwritten ORQ-compatible translations of DuckDB's join
association. They do not reproduce DuckDB's hash-join implementation,
build/probe orientation, optimizer, or DBGEN cardinality estimates. ORQ still
uses its own synthetic generator, so this isolates association changes on
identical ORQ inputs rather than comparing identical DuckDB and ORQ databases.

Before a full benchmark, run small Linux correctness checks without the
profiling flags (so ORQ's SQLite validation remains enabled):

```bash
for query in 1 3 5 8 9; do
  (cd scripts && python3 run_experiment.py -p 1 -s same -f 0.01 -T 1 \
    -m=-UEXTRA -a='-tpch-seed 20260818' "q${query}")
  (cd scripts && python3 run_experiment.py -p 1 -s same -f 0.01 -T 1 \
    -m=-UEXTRA -a='-tpch-seed 20260818' "q${query}_duckdb")
done
```

Repeat the loop with `-p 3 -s lan -c nocopy -n 4 -x node` on the configured
three-host Linux cluster. Q5 and Q9 exercise the translated compound-key joins.
An `Assertion failed`, nonzero exit, or result mismatch fails validation. The
focused automated checks are:

```bash
bash -n scripts/run-tpch-plain-and-3pc.sh
python3 -m unittest scripts.testing.test_tpch_plain_and_3pc
```

## DuckDB selectivity experiment

Run the DuckDB experiment with its SF 0.1 defaults:

```bash
python3 scripts/analyze_duckdb_tpch.py
```

For a quick smoke run, use SF 0.01 and skip transition refinement:

```bash
python3 scripts/analyze_duckdb_tpch.py \
  --scale-factor 0.01 \
  --skip-transition-refinement
```

The first run can require network access to install DuckDB's `tpch` extension.
Use `--help` for query selection, output/database paths, thread and repetition
counts, and optimized/forced mode selection.

Run the focused helper tests, and optionally the extension-backed SF 0.01 test:

```bash
python3 -m unittest discover -s scripts/testing -p 'test_*.py'
DUCKDB_TPCH_E2E=1 python3 -m unittest -v \
  scripts.testing.test_analyze_duckdb_tpch.EndToEndTests
```

## Controlled DuckDB selectivity study

Run from the repository root on the measurement host:

```bash
python3 -m venv .venv-experiments
source .venv-experiments/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s scripts/testing -p 'test*duckdb*.py' -v

# Smoke: all queries, both input modes and both fixed orders; no refinement.
python3 scripts/analyze_duckdb_tpch.py \
  --sweep-preset controlled-common --scale-factor 0.01 \
  --warmups 1 --timing-repetitions 2 --include-zero \
  --skip-transition-refinement --output-dir results/duckdb-controlled-smoke
python3 scripts/plot_duckdb_tpch.py results/duckdb-controlled-smoke

# Presentation measurements: same node, pinned DuckDB 1.5.4, one thread,
# five warmups, 30 timing repetitions, seed 20260908, up to 512 extra probes.
python3 scripts/analyze_duckdb_tpch.py \
  --sweep-preset controlled-common --scale-factor 0.1 --include-zero \
  --output-dir results/duckdb-controlled-sf01
python3 scripts/plot_duckdb_tpch.py results/duckdb-controlled-sf01

python3 scripts/analyze_duckdb_tpch.py \
  --sweep-preset controlled-common --scale-factor 1 --include-zero \
  --output-dir results/duckdb-controlled-sf1
python3 scripts/plot_duckdb_tpch.py results/duckdb-controlled-sf1
```

Each output directory and database must be new. The runner never overwrites an
existing database or nonempty artifact directory. DuckDB's TPC-H extension must
be available on the node; the existing extension helper loads or installs it.
Do not set `DUCKDB_TPCH_E2E=1` for helper-only testing: that opts into the existing
legacy end-to-end experiment test.

`--input-mode direct|materialized|both` defaults to both. `--queries` can restrict
smoke/debug runs. `--seed`, `--warmups`, `--timing-repetitions`,
`--max-transition-probes` (0–512), and `--repetitions` (separate JSON profiles)
are configurable. `--include-zero` adds explicitly labelled boundary points;
zero is not part of the 31-point common grid. Small scales can round distinct
target percentages to the same count; those targets retain distinct point IDs.

The default preset remains `legacy`. Existing commands and artifacts retain
their original behavior. For full original-predicate profiles, separately run:

```bash
python3 scripts/analyze_duckdb_tpch.py --sweep-preset legacy \
  --scale-factor 0.1 --output-dir results/duckdb-legacy-sf01
```

### Measurement and validation

Persisted `controlled_lineitem`, `controlled_orders`, and `controlled_part`
tables add ranks ordered by MD5 of `seed:primary-key[:primary-key]`, with the
primary keys breaking hash ties. The original DBGEN tables remain available for
canonical validation. Rank permutation and independent reproduction are checked
once; every measured threshold checks its actual selected count. The same
ranked table is shared by Q1/Q3 and by Q8/Q9. Decimal half-up rounding implements
`floor(target_percent * original_rows / 100 + 0.5)`.

Direct queries evaluate the rank predicate. Materialized queries use temporary
`selected_*` tables created from the identical ranked selection and analyzed
before optimization. CTAS plus ANALYZE cost is recorded once per point outside
the query timing; rank creation has separate one-time costs. Materialized and
direct queries keep the non-varied predicates defined in `scripts/duckdb_controlled.py`.

Every fixed order disables exactly `join_order,build_side_probe_side`. Both
association and child orientation are verified against the expected left-deep
tree. Q9's part→partsupp prefix adds the logically implied equality
`p.p_partkey = ps.ps_partkey`; all original join conditions remain in both
orders and in optimized SQL. Empty plans can eliminate joins; those cases are
explicitly marked as unverifiable rather than counted as verified controls.

All variants compare against the direct optimized result with absolute and
relative floating-point tolerances of 1e-8. Q3 additionally compares all groups
outside timing and checks the returned Top-N scores, allowing valid tied
membership. Canonical queries are validated against bundled TPC-H queries
separately. Controlled variants are not compared to canonical answers.

The point schedule is seeded and randomized. Within each point, every warmup
and timing block executes each control once in randomized order. Timings cover
submission through `fetchall`; optimizer-setting operations, verification,
materialization, and profiling are outside these intervals. Each median has a
seeded 2,000-resample percentile bootstrap 95% interval. These describe local
sample variation, not systematic machine noise. No end-to-end materialization
benefit is inferred from query-only timing.

Refinement first probes quarter/midpoint positions in all common-grid
intervals, including intervals with equal endpoint plans. It then bisects
observed changes to adjacent row counts, subject to the per-query/input-mode
budget. Each extra point receives the full timing/profile/control treatment.
This can take substantial time on the remote node. Search limits and unchanged
cases are recorded; absence of an observed transition is not an exhaustive
proof of invariance. Zero-row boundary controls are excluded from this search.

### Artifacts and figures

- `manifest.json`: protocol, version, machine, query definitions, rank method,
  completion/failure state, and point-manifest reference.
- `points.csv`: target and actual percentages, denominator, count, predicates,
  result checksums, validation status, three plan IDs, timings and intervals,
  estimates, intermediate work, and links to exact SQL/plans.
- `samples.csv`: every unprofiled timing, block, and randomized position.
- `operators.csv`, `joins.csv`: estimated/actual node and intermediate sizes.
- `plans/`: executable SQL, static JSON/text plans, separate analyzed JSON,
  standalone input estimates, and materialization SQL where applicable.
- `preparations.csv`, `rank_validation.json`: preparation costs and rank checks.
- `transition_search.json`: boundaries, changed dimensions, budgets, unchanged
  cases, and resolution. Association, orientation and physical structure have
  separate fingerprints; physical fingerprints include unary operators.
- `canonical_validation/` and `canonical_validation.json`: canonical SQL checks.
- `original_predicate_scenarios.json`, `original_predicate_audits.csv`: appendix
  documenting original-predicate sweeps and their attainable fractions.
- `orq_candidates.json`: observed associations with representative selectivities,
  intermediate work, timing evidence and supporting point IDs.

The plotting entry point reads only saved artifacts and does not open DuckDB.
It rejects incomplete runs, missing common-grid points, invalid baselines and
missing plan files. It exports PDF, SVG and PNG: a common-grid five-query
normalized overview with raw milliseconds, five categorical plan strips for
each fingerprint dimension, per-query explanation panels including refinement
points, before/after physical trees for observed transitions, and separate
materialization-cost plots. Each scale and input mode stays separate. Subset
runs show only the requested queries. Unknown estimates are gaps.

`figures/plotted.csv` contains plotted values and baseline point IDs;
preparation figures have companion plotted CSVs. `plan_labels.json` maps
categorical labels to full fingerprints. `figure_manifest.json` records source
SHA-256 hashes, figure names and transition-search limitations. Boundary points
remain in the raw/plotted CSV, excluded from the main comparison figures.

The normalized overview divides each median by its query's observed 100%
median. Uncertainty bands are shown in raw-millisecond and explanation panels.
Standalone input estimates isolate the rank predicate; query scan estimates
are also saved and may include pushed join filters. The intermediate-work panel
sums join-output rows; individual join outputs are available in `joins.csv`.

ORQ must measure physical table lengths, widths, sorting work, communication,
and validity counts. Secure filters do not automatically shrink tables.
DuckDB's materialized results motivate ORQ experiments; they do not establish
that ORQ obtains the same reductions cheaply. ORQ changes are outside this work.

## Rootless local 3PC WAN simulation (Linux)

The generic runner can shape loopback traffic inside a private Linux user/network
namespace. It requires Python 3, `unshare` (util-linux), `ip` and `tc` (iproute2),
permitted unprivileged user/network namespaces, and kernel netem support.
Build dependencies must already be installed because the namespace has no
external network access. `stdbuf` is also required for workload execution.

```bash
cd scripts
# Check namespace creation, shaping readback, and bounded TCP probes.
# This does not build or execute an ORQ workload.
python3 run_experiment.py -p 3 -s same -c nocopy \
  --wan-sim rootless-local --wan-sim-check micro_primitives

# Run a small shaped workload after the check succeeds.
python3 run_experiment.py -p 3 -s same -c nocopy -n 1 -T 1 -r 1024 \
  --wan-sim rootless-local micro_primitives
cd ..
```

This mode requires `-p 3 -s same -c nocopy`; MPI can bypass network shaping via
shared memory. It uses this checkout's launcher without sudo or host network
changes. The focused TPC-H runner does not support `rootless-local`.

| Option | Default |
| --- | --- |
| `--wan-latency-ms` | 6.5 ms per traversal, approximately 13 ms RTT |
| `--wan-bandwidth-gbps` | 12 Gbit/s aggregate loopback traffic, including ACKs |
| `--wan-loopback-mtu` | 1500; accepted range 1280–65536 |
| `--wan-queue-limit-packets` | Normally 13000; calculated from rate, delay, and MTU |

Latency and bandwidth must be finite and positive. The queue limit must be a
positive unsigned 32-bit integer. These options and `--wan-sim-check` apply only
to `rootless-local`. Binary arithmetic-cost hints default to `-l 6.5 -w 12`.
Compare shaped and unshaped workloads with identical inputs, threads,
repetitions, and hints; for example, use `-a='-l 6.5 -w 12'` in both runs.
Hints affect arithmetic choices and do not shape traffic.

If namespace creation is denied, inspect `unshare` stderr and ask the host
administrator about namespace policy. If netem creation fails, the administrator
may need to provide the kernel module. The runner refuses unexpected qdiscs
and does not load modules or change host sysctls. It cleans up its owned work
on catchable termination; abrupt machine failure cannot guarantee cleanup.

Measurements remain in `build/output.json`, with additional rootless profile,
probe, and qdisc-counter metadata. Counter scope covers the entire invocation.
Do not run generic experiments concurrently: they share build/output files.
This is a qualitative local latency test. Its aggregate cap, shared CPU/memory,
loopback behavior, and TCP buffers do not model independent multi-host links.
Inspect drops, backlog, and measured probes rather than assuming the rate cap
was achieved.

Optional Linux integration checks require a prebuilt `build/micro_primitives`
with `PROTOCOL=3`, `COMM=NOCOPY`, and `COMM_THREADS=1`:

```bash
ORQ_ROOTLESS_INTEGRATION=1 python3 -m unittest discover \
  -s scripts/testing -p test_rootless_wan.py
```

These checks create namespaces and run workloads. Missing dependencies,
namespace denial, or netem failure count as failures once enabled.

## Distributed 3PC WAN runs

For only distributed 3PC WAN TPC-H runs with both plan variants, omit the plaintext
and LAN cases using the focused runner's selector:

```bash
scripts/run-tpch-plain-and-3pc.sh --only-3pc-wan --plan-set both \
  --hosts zf01,zf02,zf03 --wan-mode simulated
```

Run this command from the repository root on `zf01`. The host order is party 0,
party 1, party 2. Simulated WAN requires noninteractive sudo on all three hosts.
Use `--dry-run` to inspect the ten query/variant groups. `--wan-mode real` uses the
existing network without imposing WAN delay. WAN-only reports expect only the
selected WAN cases, so omitted plaintext/LAN results are not treated as failures.

### Distributed WAN simulation without sudo

Use the userspace relay mode when the three nodes do not grant network-admin
privileges or allow unprivileged network namespaces:

```bash
scripts/run-tpch-plain-and-3pc.sh --only-3pc-wan --plan-set both \
  --hosts zf01,zf02,zf03 --wan-mode userspace
```

The generic equivalent is `-p 3 -s wan -c nocopy --hosts zf01,zf02,zf03
--wan-sim userspace-distributed`. It rebuilds NoCopy with opt-in relay support and
injects explicit `-l 6.5 -w 12` cost hints when absent. Each outgoing connection
uses a local relay, which delays both directions by 6.5 ms with a pipelined queue.
The 12 Gbit/s application-byte cap is shared across streams of each directed
party link. Queues have bounded buffering and apply backpressure. Python processing,
scheduling and extra TCP connections add overhead: this is qualitative simulation,
not packet-level netem equivalence or a throughput guarantee. ACKs, packet loss,
and ping are not shaped. All three pairwise party links carry delayed traffic.

The launcher uses ordinary SSH, Python 3 and unprivileged TCP sockets. It copies its
standalone relay worker to the same checkout path on peers. SSH control-input EOF
stops the corresponding owned party; failure stops the other parties, with five
seconds before forced termination. It never changes host qdiscs or invokes sudo.
Logs contain `[USERSPACE_WAN]` per-party counters for each repetition. The reporter
requires these counters and traffic observations and labels results `wan-userspace`.
Party 2's local relay normally has zero connections: connections 0–2 and 1–2 are
relayed at their initiating hosts, so its traffic is still delayed in both directions.

## ORQ join associations under public input reduction

The source catalog is [`tpch-selectivity-plans/manifest.json`](tpch-selectivity-plans/manifest.json). It locks DuckDB optimized/materialized
association evidence and the original frozen canonical manifest by SHA256. The
10-character `association_plan_id` is DuckDB study's SHA1 prefix, not SHA256.
No forced-order controls are used to choose candidates.

Q8's existing canonical association is `9d0bde819f` (the low-selectivity tree), and refinement
finds an additional association `350e47ddc5` at Part counts 824–865. Consequently
there are **ten new targets**, including `q8_selduckdb_d`, and 20 targets total.
The catalog records the join trees, PK/FK keys, and supporting DuckDB artifacts.

### Frozen DuckDB evidence

The selectivity runner verifies the 52 SHA-256 hashes in
`tpch-selectivity-plans/evidence.sha256`, including raw SQL and JSON plans from
`results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/`.
This generated directory is ignored by Git and is absent from a fresh clone.
Obtain the original evidence archive from the experiment coordinator and extract
it into the repository root before a dry run, correctness run, or benchmark.
The provided archive must include the `plans/` subdirectory, not just CSV summaries.

To package the evidence on the coordinator that holds the complete study:

```bash
# Run from the original experiment checkout, with all raw plans present.
python3 scripts/tpch_selectivity.py verify-evidence
tar -czf /tmp/orq-selectivity-evidence.tar.gz \
  results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1
```

To restore a supplied archive into a fresh clone and check it:

```bash
tar -xzf /path/to/orq-selectivity-evidence.tar.gz
python3 scripts/tpch_selectivity.py verify-evidence
```

A missing-file or hash-mismatch error means the frozen evidence is incomplete
or differs from the catalog. Re-running DuckDB in a new directory does not
replace these byte-identical historical artifacts. Keep the frozen catalog
unchanged when reproducing the current ORQ study. The independent DuckDB
experiments and original/canonical ORQ runner do not require this archive.

### Targets and inputs

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
and unique keys. Its `join_edges` and `base_unique_keys` entries record the join constraints;
the C++ query branches implement each invocation sequence. The runtime trace checks the association actually executed;
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

### Exact grid

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

### Protocol

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

After validating the smoke results, collect matching full-grid correctness
and then the measurements:

```bash
scripts/run-tpch-selectivity-3pc.sh --phase correctness --grid full \
  --modes same --scale-factor 0.1 --threads 16 --repetitions 1 --no-warmups \
  --output-dir results/tpch-orq-selectivity/correctness-full
scripts/run-tpch-selectivity-3pc.sh --phase benchmark --grid full \
  --modes same,lan,wan --scale-factor 0.1 --threads 16 --repetitions 3 \
  --correctness-dir results/tpch-orq-selectivity/correctness-full \
  --output-dir results/tpch-orq-selectivity/benchmark-full
``` Defaults are hosts zf01/zf02/zf03, 16 threads,
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

### Interpretation and validation

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
python3 -m unittest discover -s scripts/testing -p 'test_*.py'
cmake --build build --target test_tpch_selectivity tpch-selectivity-queries -j 2
```

Run the fixture using the same protocol/runtime environment as the build. It
checks exact rounding, MD5 vectors, nested deterministic selection, malformed
inputs, duplicate PK rejection, gathering, and compound PK/FK joins with
repeated and missing foreign keys. New result directories must pass reporter
validation before any performance comparison is presented as complete.

## Detached presentation study

### Persistent operation

Run on the Linux coordinator from the repository root. Examples use hosts `zf01,zf02,zf03`; change `--hosts` to match your cluster. The detached supervisor has its own session, disconnected stdin, persistent logs, and a heartbeat. It continues after the launching terminal disconnects while the host and its processes remain operational. A host reboot, administrator kill, or session-management policy can still terminate processes. There is no automatic reboot service and no automatic restart.

Default registry: `results/tpch-run-registry/<run-id>/`. This is outside experiment output directories and is excluded by the existing results ignore rule. It contains:

- `spec.json`: exact command, working directory, coordinator and source hashes;
- `status.json`: supervisor/runner identities, heartbeat, state, exit code;
- `supervisor.log` and `runner.log`: persistent stdout/stderr;
- archived status/spec files on an explicit resume.

Experiment outputs additionally contain immutable `manifest.json` and `schedule.json`, incremental `progress.json`, `active_process.json`, raw per-sample artifacts, reports, and presentation exports. The lock prevents concurrent selectivity workloads sharing build/output state. Do not launch the legacy runner or modify/rebuild the checkout while a study is active.

### Inspecting and recovering runs

Inspect existing runs before starting another workload:

```bash
python3 scripts/tpch_supervisor.py status
python3 scripts/tpch_supervisor.py status RUN_ID
```

This is read-only and does not contact peers or restart anything. A running job should be left running. `unknown` means inspect the coordinator/process/logs before acting. `stale` means no recorded live process was found; it does not establish successful completion. `completed` is written only after the runner succeeds and its saved report validates as complete. A live orphaned launcher blocks a duplicate start.

When done, read `report.md`, `results.json`, `presentation/slide_claims.md`, and the logs. Check completeness and qualifications before claiming a result. Never infer success merely from a missing PID or a complete-looking log tail.

To resume an interrupted run or stop an active run:

```bash
python3 scripts/tpch_supervisor.py resume RUN_ID
python3 scripts/tpch_supervisor.py stop RUN_ID
```

Resume retains the saved command and rejects source changes; the runner also validates configuration, schedule, catalog and prior samples. Successful samples are skipped. Partial attempts are archived. Stop targets verified Linux process identities using pidfds, not arbitrary PID numbers, and leaves the runner to perform its owned-process cleanup. An unresponsive process is reported; the supervisor does not silently declare it stopped.

### Presentation matrix

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

### Running the presentation study

Keep the source fixed between correctness and benchmark runs. After executable or tooling changes, generate new matching correctness evidence in a fresh directory.

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

After anchors finish, estimate remaining costs from receipts, allowing for different input sizes/new plan behavior and extra batch warmups. The historical planning scenario is about 17 hours of timed work for the whole matrix, not a bound. Target 30 hours total with six hours contingency. The detached supervisor continues without an active terminal or chat session.

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

### Exports and slides

CSV/JSON/claim exports are generated automatically at runner completion or failure when sufficient artifacts exist. Figures are a separate read-only analysis of saved results, not a query run:

```bash
python3 scripts/export_tpch_presentation.py \
  results/tpch-orq-selectivity/presentation-anchors \
  results/tpch-orq-selectivity/presentation-reduced \
  --output-dir outputs/orq-presentation-study
python3 scripts/plot_orq_presentation.py outputs/orq-presentation-study
```

Add extra-block directories to the exporter only once. It rejects duplicate blocks, unpaired candidate coverage, changed input/binary identities and incompatible provenance. It preserves the initial results separately. Complete pairs in a partial study may appear in plots; omitted/failed pairs never enter winner comparisons. Always state actual completed coverage.

Exports: `samples.csv`, `comparisons.csv`, `physical_work.csv`, `stages.csv`, `inputs.csv`, `repeat_recommendations.csv`, `slide_claims.md`, and `plot_data.json`. Charts: full-input runtime, Q8/Q9 input-size sensitivity, Q8 38.570% runtime/bytes, and plan diagrams with physical join output lengths. SVG/PDF/PNG are written to `figures/`. The plotter requires matplotlib, installed by `requirements.txt`.

Focused local implementation checks:

```bash
python3 -m unittest discover -s scripts/testing -p test_tpch_selectivity.py
python3 -m unittest discover -s scripts/testing -p test_tpch_presentation.py
```

Tests use parsed fixtures, mocked launches and harmless dummy Python processes. Do not run the general shell test harness: it can launch MPC binaries.
