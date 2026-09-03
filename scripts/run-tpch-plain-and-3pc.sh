#!/usr/bin/env bash

# Run TPC-H Q1, Q3, Q5, Q8, and Q9 with plaintext 1PC and secure 3PC.
# Distributed execution and WAN simulation are Linux-only; --dry-run is safe
# on macOS. The default prints 15 original-plan groups; --plan-set both prints
# 30 original/derived groups.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
INVOCATION_DIR="$(pwd -P)"
REPORTER="$SCRIPT_DIR/report_tpch_plain_and_3pc.py"
RUNNER="$SCRIPT_DIR/run_experiment.py"

SCALE_FACTOR="${SCALE_FACTOR:-0.1}"
THREADS="${THREADS:-16}"
REPETITIONS="${REPETITIONS:-3}"
NODE_PREFIX="${NODE_PREFIX:-node}"
WAN_MODE="${WAN_MODE:-simulated}"
PLAN_SET="${PLAN_SET:-original}"
DATA_SEED="${DATA_SEED:-20260818}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
DRY_RUN=0
QUERIES=(1 3 5 8 9)

usage() {
    cat <<'EOF'
Usage: scripts/run-tpch-plain-and-3pc.sh [options]

Run TPC-H Q1, Q3, Q5, Q8, and Q9 in this matrix:
  - plaintext 1PC, same-machine setting
  - replicated 3PC, LAN
  - replicated 3PC, simulated or real WAN

Options:
  --scale-factor VALUE   TPC-H scale factor (default: 0.1)
  --threads N            Worker threads per party (default: 16)
  --repetitions N        Runs per query/mode (default: 3)
  --node-prefix PREFIX   Hosts are PREFIX0, PREFIX1, PREFIX2 (default: node)
  --wan-mode MODE        simulated or real (default: simulated)
  --plan-set SET         original, duckdb-canonical, or both (default: original)
  --data-seed UINT64     Deterministic synthetic-data seed (default: 20260818)
  --output-dir PATH      Result directory (default: timestamped under results/)
  --dry-run              Print commands without building or connecting
  -h, --help             Show this help

Environment variables with matching uppercase names provide the same defaults;
explicit command-line options take precedence.
EOF
}

need_value() {
    if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Missing value for $1" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scale-factor)
            need_value "$@"
            SCALE_FACTOR="$2"
            shift 2
            ;;
        --threads)
            need_value "$@"
            THREADS="$2"
            shift 2
            ;;
        --repetitions)
            need_value "$@"
            REPETITIONS="$2"
            shift 2
            ;;
        --node-prefix)
            need_value "$@"
            NODE_PREFIX="$2"
            shift 2
            ;;
        --wan-mode)
            need_value "$@"
            WAN_MODE="$2"
            shift 2
            ;;
        --plan-set)
            need_value "$@"
            PLAN_SET="$2"
            shift 2
            ;;
        --data-seed)
            need_value "$@"
            DATA_SEED="$2"
            shift 2
            ;;
        --output-dir)
            need_value "$@"
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! [[ "$THREADS" =~ ^[1-9][0-9]*$ ]]; then
    echo "--threads must be a positive integer (got: $THREADS)" >&2
    exit 2
fi
if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]]; then
    echo "--repetitions must be a positive integer (got: $REPETITIONS)" >&2
    exit 2
fi
if ! [[ "$NODE_PREFIX" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "--node-prefix must contain only letters, digits, '.', '_', or '-'" >&2
    exit 2
fi
if [[ "$WAN_MODE" != "simulated" && "$WAN_MODE" != "real" ]]; then
    echo "--wan-mode must be simulated or real (got: $WAN_MODE)" >&2
    exit 2
fi
if [[ "$PLAN_SET" != "original" && "$PLAN_SET" != "duckdb-canonical" && "$PLAN_SET" != "both" ]]; then
    echo "--plan-set must be original, duckdb-canonical, or both (got: $PLAN_SET)" >&2
    exit 2
fi
if ! python3 -c 'import sys; value=int(sys.argv[1]); sys.exit(0 if 0 <= value <= (1 << 64) - 1 else 1)' "$DATA_SEED" 2>/dev/null; then
    echo "--data-seed must be an unsigned 64-bit integer (got: $DATA_SEED)" >&2
    exit 2
fi
if ! python3 -c 'import math,sys; value=float(sys.argv[1]); sys.exit(0 if math.isfinite(value) and value > 0 else 1)' "$SCALE_FACTOR" 2>/dev/null; then
    echo "--scale-factor must be a finite number greater than zero (got: $SCALE_FACTOR)" >&2
    exit 2
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$REPO_ROOT/results/tpch-plain-and-3pc/${timestamp}-SF${SCALE_FACTOR}"
elif [[ "$OUTPUT_DIR" != /* ]]; then
    OUTPUT_DIR="$INVOCATION_DIR/$OUTPUT_DIR"
fi
if [[ "$OUTPUT_DIR" == "/" ]]; then
    echo "Refusing to use / as the output directory" >&2
    exit 2
fi

CMAKE_EXTRA="-DEXTRA=-DQUERY_PROFILE=1,-DINSTRUMENT_TABLES,-DPRINT_COMMUNICATOR_STATISTICS"
FAILED=0

# run_experiment.py's established path convention is to run from scripts/ so
# that ../build and its CMake source directory resolve to this checkout.
cd "$SCRIPT_DIR"

print_command() {
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
}

run_mode() {
    local relative_dir="$1"
    local protocol="$2"
    local setting="$3"
    local communicator="$4"
    local comm_threads="$5"
    local batch_size="$6"
    local wan_sim="$7"
    local mode_dir="$OUTPUT_DIR/$relative_dir"

    local -a variants=()
    case "$PLAN_SET" in
        original) variants=(original) ;;
        duckdb-canonical) variants=(duckdb-canonical) ;;
        both) variants=(original duckdb-canonical) ;;
    esac

    for query in "${QUERIES[@]}"; do
        local variant
        for variant in "${variants[@]}"; do
            local query_name="q$query"
            local target_name="$query_name"
            local artifact_name="$query_name"
            if [[ "$variant" == "duckdb-canonical" ]]; then
                target_name="${query_name}_duckdb"
                artifact_name="${query_name}-duckdb"
            fi

            local log_file="$mode_dir/${artifact_name}.log"
            local json_file="$mode_dir/${artifact_name}.json"
            local command_status
            local -a command=(
                python3 "$RUNNER"
                -e "$REPETITIONS"
                -p "$protocol"
                -s "$setting"
                -c "$communicator"
                -f "$SCALE_FACTOR"
                -T "$THREADS"
                -b "$batch_size"
                -x "$NODE_PREFIX"
                -m="$CMAKE_EXTRA"
                "-a=--tpch-seed=$DATA_SEED"
            )
            if [[ "$communicator" == "nocopy" ]]; then
                command+=( -n "$comm_threads" )
            fi
            if [[ "$wan_sim" == "off" ]]; then
                command+=( --wan-sim off )
            fi
            command+=( "$target_name" )

            if [[ "$DRY_RUN" -eq 1 ]]; then
                print_command "${command[@]}"
                continue
            fi

            mkdir -p "$mode_dir"
            echo "Running $query_name [$variant] | SF $SCALE_FACTOR | protocol ${protocol}PC | $setting | ${THREADS} threads | ${REPETITIONS} repetitions"

            # Avoid preserving a previous query's profile if this execution fails.
            rm -f "$REPO_ROOT/build/output.json"
            set +e
            "${command[@]}" 2>&1 | tee "$log_file"
            command_status=${PIPESTATUS[0]}
            set -e

            if [[ -f "$REPO_ROOT/build/output.json" ]]; then
                cp "$REPO_ROOT/build/output.json" "$json_file"
            fi
            if [[ "$command_status" -ne 0 ]]; then
                echo "ERROR: $relative_dir/$artifact_name exited with status $command_status" >&2
                FAILED=1
            elif [[ ! -f "$json_file" ]]; then
                echo "ERROR: $relative_dir/$artifact_name produced no profile JSON" >&2
                FAILED=1
            fi
        done
    done
}

run_matrix() {
    run_mode "plaintext-1pc/same" 1 same mpi 0 -12 off
    run_mode "secure-3pc/lan" 3 lan nocopy 4 -12 off
    if [[ "$WAN_MODE" == "simulated" ]]; then
        run_mode "secure-3pc/wan-simulated" 3 wan nocopy -1 -1 auto
    else
        run_mode "secure-3pc/wan-real" 3 wan nocopy -1 -1 off
    fi
}

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY RUN working directory: $SCRIPT_DIR"
    run_matrix
    exit 0
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

preflight() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        echo "Distributed ORQ LAN/WAN runs require Linux; use --dry-run on macOS." >&2
        exit 1
    fi

    local command
    for command in python3 cmake make ssh scp startmpc tee getent ping nproc; do
        require_command "$command"
    done
    [[ -f "$RUNNER" ]] || { echo "Missing runner: $RUNNER" >&2; exit 1; }
    [[ -f "$REPORTER" ]] || { echo "Missing reporter: $REPORTER" >&2; exit 1; }

    local available_cpus
    available_cpus="$(nproc 2>/dev/null || echo 0)"
    if [[ "$available_cpus" =~ ^[0-9]+$ ]] && (( available_cpus < THREADS )); then
        echo "WARNING: $THREADS worker threads per party requested, but node0 reports $available_cpus CPUs." >&2
    fi

    local remote_repo_q remote_build_q
    printf -v remote_repo_q '%q' "$REPO_ROOT"
    printf -v remote_build_q '%q' "$REPO_ROOT/build"
    local node
    for node in "${NODE_PREFIX}0" "${NODE_PREFIX}1" "${NODE_PREFIX}2"; do
        getent hosts "$node" >/dev/null || {
            echo "Host $node does not resolve. Configure DNS or /etc/hosts first." >&2
            exit 1
        }
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$node" \
            "test -d $remote_repo_q && test -d $remote_build_q && command -v nproc >/dev/null" || {
            echo "Cannot SSH non-interactively to $node, find nproc, or find the checkout/build at the same absolute path." >&2
            exit 1
        }
        local remote_cpus
        remote_cpus="$(ssh -o BatchMode=yes "$node" nproc 2>/dev/null || echo 0)"
        if [[ "$remote_cpus" =~ ^[0-9]+$ ]] && (( remote_cpus < THREADS )); then
            echo "WARNING: $THREADS threads requested, but $node reports $remote_cpus CPUs." >&2
        fi
    done

    if [[ "$WAN_MODE" == "simulated" ]]; then
        require_command tc
        require_command ip
        sudo -n true || {
            echo "Simulated WAN requires passwordless sudo on node0." >&2
            exit 1
        }
        for node in "${NODE_PREFIX}1" "${NODE_PREFIX}2"; do
            ssh -o BatchMode=yes "$node" 'command -v python3 >/dev/null && command -v ip >/dev/null && command -v tc >/dev/null && sudo -n true' || {
                echo "Simulated WAN requires Python 3, ip, tc, and passwordless sudo on $node." >&2
                exit 1
            }
        done
    fi
}

write_metadata() {
    {
        echo "TPC-H queries: ${QUERIES[*]}"
        echo "Scale factor: $SCALE_FACTOR"
        echo "Threads per party: $THREADS"
        echo "Repetitions: $REPETITIONS"
        echo "Node prefix: $NODE_PREFIX"
        echo "WAN mode: $WAN_MODE"
        echo "Plan set: $PLAN_SET"
        echo "TPC-H data seed: $DATA_SEED"
        if [[ "$WAN_MODE" == "simulated" ]]; then
            echo "WAN simulator: 12 Gbit/s, 6.5 ms one-way delay (approximately 13 ms RTT)"
        else
            echo "WAN simulator: disabled; host network conditions are used unchanged"
        fi
        echo "Generated: $(date --iso-8601=seconds)"
        echo "Git commit: $(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        echo "Git branch: $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
        echo
        echo "Coordinator:"
        hostname
        uname -a
        lscpu 2>/dev/null || true
        ip -brief address 2>/dev/null || true
        echo
        echo "Initial server-to-server RTT from coordinator:"
        local node
        for node in "${NODE_PREFIX}1" "${NODE_PREFIX}2"; do
            ping -q -c 3 "$node" 2>&1 || true
        done
        echo
        echo "Remote nodes:"
        for node in "${NODE_PREFIX}0" "${NODE_PREFIX}1" "${NODE_PREFIX}2"; do
            echo "[$node]"
            ssh -o BatchMode=yes "$node" 'hostname; uname -srmo; command -v nproc >/dev/null && nproc' 2>&1 || true
        done
    } > "$OUTPUT_DIR/meta.txt"
}

cleanup_wan_after_signal() {
    if [[ "$WAN_MODE" == "simulated" ]]; then
        echo "Interrupted; attempting to remove WAN traffic shaping." >&2
        "$SCRIPT_DIR/comm/cluster-wan-sim.sh" off "${NODE_PREFIX}1" "${NODE_PREFIX}2" || true
    fi
}

trap 'cleanup_wan_after_signal; exit 130' INT TERM

preflight
if [[ "$WAN_MODE" == "simulated" ]]; then
    echo "Removing any stale WAN traffic shaping before the 3PC LAN baseline."
    "$SCRIPT_DIR/comm/cluster-wan-sim.sh" off "${NODE_PREFIX}1" "${NODE_PREFIX}2"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd)"
write_metadata
run_matrix

set +e
python3 "$REPORTER" \
    --output-dir "$OUTPUT_DIR" \
    --scale-factor "$SCALE_FACTOR" \
    --threads "$THREADS" \
    --repetitions "$REPETITIONS" \
    --node-prefix "$NODE_PREFIX" \
    --wan-mode "$WAN_MODE" \
    --plan-set "$PLAN_SET" \
    --data-seed "$DATA_SEED"
REPORT_STATUS=$?
set -e

echo "Finished. Report: $OUTPUT_DIR/report.md"
echo "Structured results: $OUTPUT_DIR/results.json"

if [[ "$FAILED" -ne 0 || "$REPORT_STATUS" -ne 0 ]]; then
    exit 1
fi
