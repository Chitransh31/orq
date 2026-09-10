# Implementation validation record

The implementation adds ten association targets, deterministic public reductions,
a frozen evidence catalog, actual PK/FK trace checks, and a paired runner/reporter.
This record distinguishes implementation checks from experimental performance
claims. The full SF 0.1 performance matrix has not been run.

Completed checks:

- All 20 targets compiled in plaintext correctness configuration.
- C++ deterministic selection/digest/rounding and compound PK/FK join fixture
  passed, including duplicate/missing foreign keys.
- The initial finalized smoke correctness matrix passed all 20 targets at
  SF 0.01, 16 threads, data seed20260818. Results are in
  `results/tpch-orq-selectivity/correctness-smoke/`. The runner/reporter later
  gained explicit WAN hint arguments; this older source manifest remains
  historical evidence and is intentionally not accepted as current benchmark
  authorization.
- All 20 targets passed an additional SF 0.01 correctness check with data
  seed20260819 and 100% inputs. Every query produced nonempty results, including
  all Q8 alternatives and Q9 compound-key alternatives. Input hashes and exact
  logical output bytes matched across variants. Artifacts are in
  `results/tpch-orq-selectivity/correctness-extra-seed/`.
- Direct Q8 checks at 4.225% and 100% Part input also matched across all six
  targets. Q8 and Q9 nonvaried input hashes and original varied-input hashes
  were unchanged across reductions.
- Every new target's supporting point IDs were checked against `points.csv`:
  each is optimized/materialized evidence with the exact association string.
  Catalog validation verifies 52 SHA256 artifact hashes and the existing
  canonical normalized-plan checks.

- Full-grid SF 0.01 plaintext correctness passed **all 182 target/point
  combinations**, with 16 threads, matching result bytes, cross-point input
  stability and nested selections. The report is
  `results/tpch-orq-selectivity/correctness-full-sf0p01/report.md`.
- The final Python suite passed **110 tests**, with four existing skips. Both
  shell runners pass `bash -n`, and `git diff --check` passes.
- A fresh final-source SF 0.01 plaintext smoke passed **all 20 targets** after
  the interrupted-attempt recovery fix. Its report is complete with no
  validation failures, and its 156-entry source-hash manifest covers the code
  used for the run. See
  `results/tpch-orq-selectivity/correctness-smoke-final/report.md`.
- All **20 LAN correctness targets passed** on zf01/zf02/zf03, with three
  party scopes and validated byte accounting. Their input hashes and result
  bytes also match the corresponding plaintext proof exactly. Reports are in
  `results/tpch-orq-selectivity/correctness-lan-smoke/`; the cross-mode audit is
  `results/tpch-orq-selectivity/cross-mode-correctness.json`.
- All **20 profiled LAN smoke targets passed** the input, association, stage,
  intermediate-size and communication validations. This was one repetition
  without warmups, so `results/tpch-orq-selectivity/benchmark-lan-smoke/` is an
  integration result rather than evidence for choosing a winner.
- The userspace WAN pilot passed for Q8 original, canonical and refinement-only
  target `q8_selduckdb_d`. All three samples used explicit `-l 6.5 -w 12`
  cost hints, matched correctness inputs, and recorded healthy relay diagnostics.
  See `results/tpch-orq-selectivity/wan-pilot/pilot.json`.
- An interrupted LAN attempt exposed a pre-receipt recovery gap. The runner now
  archives partial owned artifacts even when no receipt exists, with a focused
  regression test. Earlier reports remain valid historical validation artifacts,
  but strict source-hash checks intentionally do not authorize them for a new
  benchmark after this runner-only fix.

Validation in progress: none. The full experiment remains deliberately unrun.

The final JSON reports, rather than the presence of a directory, establish
completion. Failed development attempts are retained separately. An early Q1
run stopped because its otherwise successful SQLite check lacked the completion
marker; the marker was added and all 20 smoke targets subsequently passed.
An early Q8 reduced Part loader loop used the original SQLite insertion length;
it was corrected to insert exactly the retained rows before the successful checks.

Remaining experimental work after these gates:

- SF 0.1 correctness on the full grid with the final source manifest.
- Three-repetition SF 0.1 1PC/LAN/WAN matrix (1,638 measured launches plus warmups),
  followed by observed runtime/communication wins and losses and any additional
  paired repetitions justified by dispersion.
- A separate secure filtering/compaction pipeline study, if desired; public
  pre-sharing reduction does not measure that cost.

Schedules stay immutable. At small SF some adjacent source boundaries round to
identical ORQ inputs; the report records their actual retained lengths. Additional
paired repetition blocks use a separately declared run rather than changing a
schedule after observing winners.
