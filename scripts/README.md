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

## Focused ORQ TPC-H benchmark

For complete three-server installation, SSH, correctness, LAN/WAN, recovery,
and troubleshooting instructions, see
[`LINUX_3PC_TPCH_SETUP.md`](../LINUX_3PC_TPCH_SETUP.md).

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
    -m=-UEXTRA -a=--tpch-seed=20260818 "q${query}")
  (cd scripts && python3 run_experiment.py -p 1 -s same -f 0.01 -T 1 \
    -m=-UEXTRA -a=--tpch-seed=20260818 "q${query}_duckdb")
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
