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

## Rootless local 3PC WAN simulation (Linux)

The generic runner supports a private user/network namespace with loopback netem shaping:

```bash
cd scripts # from the repository root
python3 run_experiment.py -p 3 -s same -c nocopy -n 1 -T 1 -r 1024 \
  --wan-sim rootless-local micro_primitives

# Actual namespace creation, qdisc readback and bounded TCP probes; no build or ORQ run.
python3 run_experiment.py -p 3 -s same -c nocopy \
  --wan-sim rootless-local --wan-sim-check micro_primitives
```

This requires Linux, Python 3, `unshare` (util-linux), `ip` and `tc` (iproute2),
unprivileged user/network namespace support, and kernel netem support. Workloads also
need the normal build tools, `stdbuf`, and all build dependencies already available
locally: the private network namespace has no external connectivity. Rootless mode
uses this checkout's hardened `startmpc` explicitly. Ping is optional. No `sudo`,
host sysctl changes, automatic module loading, or host qdisc changes are performed.

The mode requires `-p 3 -s same -c nocopy`; `same` otherwise defaults to MPI, whose
shared-memory transport can bypass shaping. Existing `auto` and `off` modes retain
their behavior. The focused TPC-H runner does not support this new mode yet.

| Option | Default |
| --- | --- |
| `--wan-latency-ms` | 6.5 ms per loopback traversal, approximately 13 ms RTT |
| `--wan-bandwidth-gbps` | 12 Gbit/s aggregate loopback traffic, including ACKs |
| `--wan-loopback-mtu` | 1500; accepted range 1280–65536 |
| `--wan-queue-limit-packets` | `max(1000, ceil(2 * rate_bytes_per_second * delay_seconds / MTU))`, normally 13000 |

Latency and bandwidth must be finite and positive. The queue limit must fit a
positive unsigned 32-bit integer; actual tool setup/readback is also required.
These options and `--wan-sim-check` are valid only with `rootless-local`.
Conflicting CMake protocol/communicator overrides and binary setting overrides
are rejected before execution, and the configured CMake cache is checked before
building. Configuration cannot be redirected with `-B`, `-S`, or CMake script mode.

Binary cost-model hints default to `-l 6.5 -w 12` (or the selected physical profile).
One explicit positive hint per dimension is preserved, with a warning if it differs
from shaping. The binary's supported long spellings are `-latency`, `-bandwidth`,
and `-setting`, using a **single dash** and a separate value. Argument files are
validated too. Hints influence RCA/PPA choices; they do not shape traffic. When
comparing unshaped and shaped workloads, use identical binary, inputs, threads,
repetitions, and explicit hints in both, for example `-a='-l 6.5 -w 12'`.

Results keep the existing concatenated-JSON `build/output.json` format. Optional
`config.rootless` records the physical profile, effective MTU/qdisc, cost-model
hints and managed shaping state. `rootless_invocation` contains before/after qdisc
counters and available probe results; its scope is the **full invocation**, not one
repetition. Dynamic counters do not enter grouping keys. Diagnostics are also
printed, including when setup or execution fails. Shared build/output files make
concurrent generic-runner invocations unsafe even with isolated networking.

If `unshare` fails, inspect its exit status and stderr and ask the site administrator
about user-namespace limits or security restrictions. A container does not guarantee
a workaround. A missing netem module listing is inconclusive; successful qdisc
creation is the support check. If `tc` cannot create netem, an administrator may need
to provide/load `sch_netem`. Unexpected qdiscs are refused, never replaced. A namespace
identity failure stops before interface mutation. Setup errors include the failed
command and requested profile. No distributions or kernel configurations have been
certified by this change; run the check on the intended host.

The helper forwards SIGINT, SIGTERM and SIGHUP to owned work, allowing five seconds
before SIGKILL escalation, and reaps descendants before removing its owned qdisc.
The local launcher likewise signals only owned party processes/groups, including
on a party failure. Catchable termination returns `128 + signal`; cleanup errors
make success nonzero without hiding a workload failure. SIGKILL, interpreter crashes,
and machine failure cannot guarantee cleanup. Namespace destruction removes the
qdisc only after all namespace references disappear; surviving processes can retain
it, but it cannot become a host qdisc.

This is a qualitative latency-sensitivity and functional test, not a substitute for
representative multi-host measurements. The rate is one aggregate cap, not three
independent links; do not substitute 36 Gbit/s. All parties share CPU, memory,
caches and scheduling. At the defaults, one-way queued data is about 9.75 MB and
full-RTT bandwidth-delay product is about 19.5 MB. TCP buffer limits may prevent
filling the rate; this mode does not tune them. The queue heuristic gives burst
headroom, not a loss-free guarantee: inspect drops/backlog. MTU 1500 does not remove
loopback offload differences. Kernel timer granularity, bursts and TCP Small Queues
also affect results. The [netem manual](https://man7.org/linux/man-pages/man8/tc-netem.8.html)
recommends receiver-ingress placement for realistic TCP measurements; this topology
does not recreate independent receiver links.

Run ordinary regression tests with:

```bash
python3 -m unittest discover -s scripts/testing -p 'test_*.py' # from repository root
```

The network integration tests are disabled by default. To enable them, first build
`build/micro_primitives` with `PROTOCOL=3`, `COMM=NOCOPY`, and `COMM_THREADS=1` using
the repository's normal dependency configuration, then run:

```bash
ORQ_ROOTLESS_INTEGRATION=1 python3 -m unittest discover \
  -s scripts/testing -p test_rootless_wan.py
```

Once enabled, missing dependencies, missing binary, namespace denial, and netem
failure are failures, not skips. Coverage includes actual setup/readback, a default
added median TCP RTT window of 8–40 ms, identical shaped/unshaped NoCopy workloads,
increasing qdisc traffic counters, party failure, signals, descendant cleanup,
unrelated-process survival, and unchanged host qdisc configuration. Runtime ratios
are diagnostic only. The probe has a 30-second deadline; reports outside the RTT
window include host load.

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

## Selectivity-dependent ORQ associations

`run-tpch-selectivity-3pc.sh` runs all fixed associations on identical public
pre-sharing reductions. It requires validated correctness evidence before
benchmarking and uses the existing local/cluster launcher. See
[the experiment protocol](tpch-selectivity-plans/README.md) for exact grids,
new targets, smoke/full commands, provenance and interpretation limits.
The original full-input runner and frozen canonical manifest are unchanged.

### Detached presentation study (implementation only)

The `presentation` grid adds full-input anchors and selected smaller-input comparisons: 53 combinations / 159 measured WAN executions at three repetitions. `scripts/tpch_supervisor.py` provides explicit detached `start`, read-only `status`, explicit `resume`, and graceful `stop`. Jobs do not depend on an active assistant session. Nothing starts automatically.

Read [TPCH_PRESENTATION_RUNBOOK.md](TPCH_PRESENTATION_RUNBOOK.md) before any launch or recovery. Existing jobs are discoverable with `python3 scripts/tpch_supervisor.py status` from the repository root. New report exports and plots are provided by `export_tpch_presentation.py` and `plot_orq_presentation.py`; historical results remain separate. Do not run experimental correctness or performance workloads until explicitly requested.
