#!/usr/bin/env python3
"""Build JSON and Markdown reports for the focused ORQ TPC-H benchmark matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


ORIGINAL_QUERY_PLANS: dict[str, dict[str, Any]] = {
    "q1": {
        "source": "bench/queries/tpch/q1.cpp",
        "steps": [
            "Scan Lineitem and project ship date, group keys, price, discount, tax, and quantity.",
            "Filter ShipDate, then sort by validity, ReturnFlag, and LineStatus.",
            "Compute discounted price and charge columns.",
            "Aggregate sums and counts by validity, ReturnFlag, and LineStatus.",
            "Sort valid groups, retain the bounded result, and compute averages.",
            "Finalize the oblivious result.",
        ],
    },
    "q3": {
        "source": "bench/queries/tpch/q3.cpp",
        "steps": [
            "Scan and filter Customer by MktSegment, Orders by OrderDate, and Lineitem by ShipDate.",
            "Compute Lineitem revenue.",
            "Join Customer to Orders on CustKey.",
            "Join the result to Lineitem on OrderKey while summing revenue per order.",
            "Convert revenue for sorting, sort by validity, revenue descending, and OrderDate.",
            "Keep the top ten rows and finalize the oblivious result.",
        ],
    },
    "q5": {
        "source": "bench/queries/tpch/q5.cpp",
        "steps": [
            "Filter Orders by date and Region by name; join Region to Nation.",
            "Compute Lineitem revenue.",
            "Build the supplier branch: selected nations to Supplier to Lineitem.",
            "Build the customer branch: Nation to Customer to Orders to Lineitem.",
            "Join both branches on OrderKey, LineNumber, and nation Name.",
            "Aggregate revenue by nation, bound the valid groups, sort by revenue descending, and finalize.",
        ],
    },
    "q8": {
        "source": "bench/queries/tpch/q8.cpp",
        "steps": [
            "Filter Part by type, Region by name, and Orders by date; derive the year bucket.",
            "Compute Lineitem volume.",
            "Join Part to Lineitem to Orders to Customer to customer Nation to Region.",
            "Join Supplier and supplier Nation to complete the nation attribution.",
            "Compute nation-filtered volume and aggregate total/filtered volume by year.",
            "Bound valid groups, privately divide to obtain market share, and finalize.",
        ],
    },
    "q9": {
        "source": "bench/queries/tpch/q9.cpp",
        "steps": [
            "Filter Part by the encoded color/name predicate.",
            "Join Nation to Supplier, then Supplier to Lineitem.",
            "Join the filtered Part, then Orders, then PartSupp on PartKey and SuppKey.",
            "Compute profit amount from price, discount, supply cost, and quantity.",
            "Aggregate profit by nation and order date.",
            "Sort by nation ascending and order date descending, then finalize.",
        ],
    },
}

# Backward-compatible name used by the original report tests and callers.
QUERY_PLANS = ORIGINAL_QUERY_PLANS

DUCKDB_ORQ_STEPS: dict[str, list[str]] = {
    "q1": [
        "No-op control: execute the original ORQ scan, filter, sort, aggregation, and averages.",
        "DuckDB's perfect-hash aggregation is not an ORQ operator and is not translated.",
    ],
    "q3": [
        "Filter Customer, Orders, and Lineitem and compute revenue as in the original query.",
        "Join Customer to Orders, then join Lineitem and aggregate revenue per order.",
        "Treat DuckDB's build/probe orientation as non-portable; the ORQ association is unchanged.",
    ],
    "q5": [
        "Join filtered Region to Nation, then join Customer and filtered Orders.",
        "Join Lineitem while carrying nation name and nation key.",
        "Join Supplier last on SuppKey and NationKey, with Supplier as ORQ's primary-key side.",
        "Aggregate and sort as in the original query.",
    ],
    "q8": [
        "Build the Part-Lineitem-Orders-Customer branch.",
        "Build the filtered Region-customer-Nation branch separately and combine both branches.",
        "Join Supplier and supplier Nation, then calculate and aggregate market share as before.",
    ],
    "q9": [
        "Build the filtered Part-Lineitem-Orders branch.",
        "Build the Nation-Supplier-PartSupp branch.",
        "Join both branches on the PartKey-SuppKey composite key with PartSupp as the unique side.",
        "Calculate, aggregate, and sort profit as in the original query.",
    ],
}

PLAN_SET_VARIANTS = {
    "original": ("original",),
    "duckdb-canonical": ("duckdb-canonical",),
    "both": ("original", "duckdb-canonical"),
}
PLAN_MANIFEST = Path(__file__).resolve().parent / "tpch-duckdb-plans" / "manifest.json"

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PROFILE_RE = re.compile(rf"\[=PROFILE\]\s+(\S+)\s+({NUMBER})\s+sec")
COMM_TOTAL_RE = re.compile(r"\bP(\d+)\s+Total\s+(\d+)\s*$")
RTT_RE = re.compile(rf"^\s*(\S+):\s+({NUMBER})\s+ms\s*$")
PLAN_MARKER_RE = re.compile(
    r"\[QUERY_PLAN\]\s+query=(\S+)\s+variant=(\S+)(?:\s+association=(.*))?$"
)


def load_plan_catalog(manifest_path: Path = PLAN_MANIFEST) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_dir = manifest_path.parent
    duckdb_queries: dict[str, Any] = {}
    for query, entry in manifest["queries"].items():
        explain_path = plan_dir / entry["explain_json"]
        sql_path = plan_dir / entry["sql"]
        raw_explain = json.loads(explain_path.read_text(encoding="utf-8"))
        normalized = json.dumps(raw_explain, separators=(",", ":")) + "\n"
        observed_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        expected_hash = entry["normalized_explain_sha256"]
        if observed_hash != expected_hash:
            raise ValueError(
                f"frozen DuckDB plan hash mismatch for {query}: "
                f"expected {expected_hash}, observed {observed_hash}"
            )
        duckdb_queries[query] = {
            **entry,
            "sql_file": str(sql_path.relative_to(manifest_path.parents[1])),
            "explain_json_file": str(explain_path.relative_to(manifest_path.parents[1])),
            "canonical_sql": sql_path.read_text(encoding="utf-8"),
            "raw_explain_json": raw_explain,
            "orq_steps": DUCKDB_ORQ_STEPS[query],
        }

    return {
        "schema_version": manifest["schema_version"],
        "plan_kind": manifest["plan_kind"],
        "duckdb_source": manifest["source"],
        "portability": manifest["portability"],
        "variants": {
            "original": {
                "kind": "Fixed handwritten ORQ C++ physical plans",
                "queries": ORIGINAL_QUERY_PLANS,
            },
            "duckdb-canonical": {
                "kind": "Fixed handwritten ORQ-compatible translations of canonical DuckDB plans",
                "queries": duckdb_queries,
            },
        },
    }


def calculate_statistics(values: Iterable[float | int]) -> dict[str, float | int | None]:
    samples = [float(value) for value in values if math.isfinite(float(value))]
    if not samples:
        return {"mean": None, "median": None, "min": None, "max": None,
                "stdev": None, "count": 0}
    return {
        "mean": round(statistics.mean(samples), 6),
        "median": round(statistics.median(samples), 6),
        "min": round(min(samples), 6),
        "max": round(max(samples), 6),
        "stdev": round(statistics.stdev(samples), 6) if len(samples) > 1 else None,
        "count": len(samples),
    }


def load_json_documents(path: Path) -> list[Any]:
    """Read either normal JSON or run_experiment.py's concatenated JSON objects."""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            break
        document, offset = decoder.raw_decode(text, offset)
        documents.append(document)
    return documents


def _groups(documents: Iterable[Any]) -> Iterator[dict[str, Any]]:
    for document in documents:
        if isinstance(document, dict):
            yield document
        elif isinstance(document, list):
            for item in document:
                if isinstance(item, dict):
                    yield item


def _operator_event(line: str, repetition: int) -> dict[str, Any] | None:
    match = re.search(r"\[TABLE_JOIN\]\s+L=(\d+)\s+R=(\d+)\s+k=(\d+)\s+a=(\d+)", line)
    if match:
        return {
            "repetition": repetition,
            "operator": "join",
            "left_rows": int(match.group(1)),
            "right_rows": int(match.group(2)),
            "key_columns": int(match.group(3)),
            "aggregations": int(match.group(4)),
        }
    match = re.search(r"\[TABLE_SORT\]\s+(\S+)\s+k=(\d+)\s+n=(\d+)", line)
    if match:
        return {
            "repetition": repetition,
            "operator": "sort",
            "algorithm": match.group(1),
            "key_columns": int(match.group(2)),
            "input_rows": int(match.group(3)),
        }
    match = re.search(r"\[TABLE_AGG\]\s+k=(\d+)\s+n=(\d+)\s+a=(\d+)", line)
    if match:
        return {
            "repetition": repetition,
            "operator": "aggregate",
            "key_columns": int(match.group(1)),
            "input_rows": int(match.group(2)),
            "aggregations": int(match.group(3)),
        }
    match = re.search(r"\[PRIV_DIV\]\s+n=(\d+)", line)
    if match:
        return {
            "repetition": repetition,
            "operator": "private_division",
            "input_rows": int(match.group(1)),
        }
    return None


def parse_log(text: str) -> dict[str, Any]:
    repetition = -1
    operator_events: list[dict[str, Any]] = []
    profile_samples: dict[str, list[float]] = defaultdict(list)
    communicator_samples: dict[str, list[int]] = defaultdict(list)
    rtt_samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    plan_markers: list[dict[str, str | None]] = []
    userspace_relays: list[dict[str, Any]] = []
    rtt_section: str | None = None

    for line in text.splitlines():
        if line.startswith("[USERSPACE_WAN] "):
            try:
                userspace_relays.append(json.loads(line[len("[USERSPACE_WAN] "):]))
            except json.JSONDecodeError:
                userspace_relays.append({"errors": ["invalid relay diagnostic JSON"]})
        if re.search(r"\[=SW\]\s+Start\s*$", line):
            repetition += 1

        event = _operator_event(line, max(repetition, 0))
        if event is not None:
            operator_events.append(event)

        match = PROFILE_RE.search(line)
        if match:
            profile_samples[match.group(1)].append(float(match.group(2)))

        match = COMM_TOTAL_RE.search(line)
        if match:
            communicator_samples[f"party_{match.group(1)}"].append(int(match.group(2)))

        match = PLAN_MARKER_RE.search(line)
        if match:
            plan_markers.append({
                "query": match.group(1),
                "variant": match.group(2),
                "association": match.group(3),
            })

        if "Baseline ping latencies" in line:
            rtt_section = "baseline"
            continue
        if "Ping latencies after enabling" in line:
            rtt_section = "simulated_wan"
            continue
        if "Ping latencies after disabling" in line:
            rtt_section = "restored"
            continue

        match = RTT_RE.match(line)
        if match and rtt_section is not None:
            rtt_samples[rtt_section][match.group(1)].append(float(match.group(2)))

    return {
        "operator_events": operator_events,
        "operator_profile_seconds": {
            label: calculate_statistics(values) for label, values in sorted(profile_samples.items())
        },
        "communicator_bytes_sent": {
            party: calculate_statistics(values)
            for party, values in sorted(communicator_samples.items())
        },
        "network_rtt_ms": {
            section: {
                host: calculate_statistics(values) for host, values in sorted(hosts.items())
            }
            for section, hosts in sorted(rtt_samples.items())
        },
        "query_plan_markers": plan_markers,
        "userspace_relays": userspace_relays,
    }


def _stage_data(documents: list[Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    config: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    for group in _groups(documents):
        if not config and isinstance(group.get("config"), dict):
            config = group["config"]
        raw_data = group.get("raw_data", [])
        if isinstance(raw_data, list):
            samples.extend(item for item in raw_data if isinstance(item, dict))

    labels: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        for label, value in sample.items():
            if isinstance(label, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                labels[label].append(float(value))
    return config, samples, {
        label: calculate_statistics(values) for label, values in sorted(labels.items())
    }


def _average_host_rtt(section: dict[str, dict[str, Any]]) -> float | None:
    means = [entry.get("mean") for entry in section.values() if isinstance(entry, dict)]
    numeric = [float(value) for value in means if value is not None]
    return statistics.mean(numeric) if numeric else None


def _artifact_name(query: str, variant: str) -> str:
    return query if variant == "original" else f"{query}-duckdb"


def _artifact_identity(name: str) -> tuple[str, str]:
    if name.endswith("-duckdb"):
        return name.removesuffix("-duckdb"), "duckdb-canonical"
    return name, "original"


def _expected_modes(wan_mode: str, only_3pc_wan: bool = False) -> tuple[Path, ...]:
    if only_3pc_wan and wan_mode == "none":
        raise ValueError("WAN-only reporting requires simulated, userspace or real WAN")
    modes = [] if only_3pc_wan else [Path("plaintext-1pc/same"), Path("secure-3pc/lan")]
    if wan_mode != "none":
        modes.append(Path(f"secure-3pc/wan-{wan_mode}"))
    return tuple(modes)


def _find_run_files(
    output_dir: Path, wan_mode: str, plan_set: str, only_3pc_wan: bool = False
) -> Iterator[tuple[Path, Path, Path]]:
    selected_variants = set(PLAN_SET_VARIANTS[plan_set])
    expected_modes = _expected_modes(wan_mode, only_3pc_wan)

    def selected(path: Path) -> bool:
        query, variant = _artifact_identity(path.stem)
        try:
            relative_parent = path.parent.relative_to(output_dir)
        except ValueError:
            return False
        return (
            relative_parent in expected_modes
            and query in QUERY_PLANS
            and variant in selected_variants
        )

    json_files = {
        path.with_suffix("") for path in output_dir.rglob("q*.json") if selected(path)
    }
    log_files = {
        path.with_suffix("") for path in output_dir.rglob("q*.log") if selected(path)
    }
    expected_files = {
        output_dir / mode / _artifact_name(query, variant)
        for mode in expected_modes
        for query in QUERY_PLANS
        for variant in PLAN_SET_VARIANTS[plan_set]
    }
    for stem in sorted(json_files | log_files | expected_files):
        yield stem, stem.with_suffix(".json"), stem.with_suffix(".log")


def _configured_data_seed(config: dict[str, Any]) -> int | None:
    args = config.get("exp_args", [])
    if not isinstance(args, list):
        return None
    for index, arg in enumerate(args):
        if arg in ("-tpch-seed", "-z") and index + 1 < len(args):
            try:
                return int(args[index + 1])
            except (ValueError, TypeError):
                return None
        if isinstance(arg, str) and arg.startswith("--tpch-seed="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return None
    return None


def build_report_data(
    output_dir: Path,
    scale_factor: float,
    threads: int,
    repetitions: int,
    node_prefix: str,
    wan_mode: str,
    plan_set: str = "original",
    data_seed: int = 20260818,
    hosts: list[str] | None = None,
    only_3pc_wan: bool = False,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    plan_catalog = load_plan_catalog()

    for stem, json_path, log_path in _find_run_files(output_dir, wan_mode, plan_set, only_3pc_wan):
        query, variant = _artifact_identity(stem.name)
        relative_parent = stem.parent.relative_to(output_dir)
        mode = str(relative_parent)
        issues: list[str] = []
        config: dict[str, Any] = {}
        samples: list[dict[str, Any]] = []
        stage_statistics: dict[str, Any] = {}

        if json_path.exists():
            try:
                config, samples, stage_statistics = _stage_data(load_json_documents(json_path))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                issues.append(f"invalid profile JSON: {error}")
        else:
            issues.append("missing profile JSON")

        log_data: dict[str, Any] = {
            "operator_events": [],
            "operator_profile_seconds": {},
            "communicator_bytes_sent": {},
            "network_rtt_ms": {},
            "query_plan_markers": [],
        }
        if log_path.exists():
            try:
                log_data = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
            except OSError as error:
                issues.append(f"unreadable log: {error}")
        else:
            issues.append("missing raw log")

        if mode == "secure-3pc/wan-simulated":
            baseline = _average_host_rtt(log_data["network_rtt_ms"].get("baseline", {}))
            simulated = _average_host_rtt(
                log_data["network_rtt_ms"].get("simulated_wan", {})
            )
            if baseline is None or simulated is None:
                issues.append("missing simulated-WAN RTT verification")
            elif simulated <= baseline:
                issues.append(
                    f"simulated-WAN RTT did not increase ({baseline:.3f} ms to {simulated:.3f} ms)"
                )

        if mode == "secure-3pc/wan-userspace":
            relays = log_data.get("userspace_relays", [])
            if any(item.get("errors") for item in relays):
                issues.append("userspace relay reported errors")
            for rank in range(3):
                observations = [item for item in relays if item.get("rank") == rank]
                if len(observations) != repetitions:
                    issues.append(f"expected {repetitions} relay diagnostics for party {rank}, found {len(observations)}")
                if rank < 2 and any(item.get("bytes", 0) <= 0 or item.get("connections", 0) <= 0 for item in observations):
                    issues.append(f"party {rank} has no observed relayed traffic")
            if config.get("wan_sim") != "userspace-distributed":
                issues.append("profile does not identify userspace WAN mode")

        overall_count = stage_statistics.get("Overall", {}).get("count", 0)
        if overall_count != repetitions:
            issues.append(f"expected {repetitions} Overall samples, found {overall_count}")

        configured_seed = _configured_data_seed(config)
        if configured_seed is not None and configured_seed != data_seed:
            issues.append(
                f"profile used data seed {configured_seed}, expected {data_seed}"
            )
        elif configured_seed is None and plan_set != "original":
            issues.append("profile JSON does not record --tpch-seed")

        if plan_set != "original":
            expected_marker_variant = (
                "original-orq" if variant == "original" else "duckdb-canonical"
            )
            markers = log_data["query_plan_markers"]
            if not markers:
                issues.append("raw log does not contain a [QUERY_PLAN] marker")
            elif any(
                marker["query"] != query
                or marker["variant"] != expected_marker_variant
                for marker in markers
            ):
                issues.append(
                    f"raw log plan marker does not match {query}/{expected_marker_variant}"
                )
            elif variant == "duckdb-canonical":
                expected_association = plan_catalog["variants"][variant]["queries"][query][
                    "association_tree"
                ]
                if any(
                    marker["association"] != expected_association for marker in markers
                ):
                    issues.append(
                        "raw log DuckDB association does not match the frozen manifest"
                    )

        status = "success" if not issues else "incomplete"
        if issues:
            failures.append({
                "mode": mode,
                "query": query,
                "variant": variant,
                "reason": "; ".join(issues),
            })

        runs.append({
            "mode": mode,
            "query": query,
            "variant": variant,
            "target": query if variant == "original" else f"{query}_duckdb",
            "status": status,
            "issues": issues,
            "config": config,
            "profile_json": str(json_path.relative_to(output_dir)) if json_path.exists() else None,
            "raw_log": str(log_path.relative_to(output_dir)) if log_path.exists() else None,
            "repetition_metrics": samples,
            "stage_statistics_seconds": stage_statistics,
            **log_data,
        })

    metadata_file = output_dir / "meta.txt"
    metadata_text = metadata_file.read_text(encoding="utf-8", errors="replace") \
        if metadata_file.exists() else ""
    profiled_executions = sum(
        run["stage_statistics_seconds"].get("Overall", {}).get("count", 0) for run in runs
    )
    successful_executions = sum(
        run["stage_statistics_seconds"].get("Overall", {}).get("count", 0)
        for run in runs
        if run["status"] == "success"
    )
    comparisons = build_comparisons(runs) if plan_set == "both" else []
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": {
            "queries": list(QUERY_PLANS),
            "scale_factor": scale_factor,
            "threads": threads,
            "repetitions": repetitions,
            "plan_set": plan_set,
            "plan_variants": list(PLAN_SET_VARIANTS[plan_set]),
            "data_seed": data_seed,
            "expected_executions": (
                len(QUERY_PLANS)
                * len(PLAN_SET_VARIANTS[plan_set])
                * len(_expected_modes(wan_mode, only_3pc_wan))
                * repetitions
            ),
            "profiled_executions": profiled_executions,
            "successful_executions": successful_executions,
            "node_prefix": node_prefix,
            "hosts": hosts or [f"{node_prefix}{index}" for index in range(3)],
            "wan_mode": wan_mode,
            "only_3pc_wan": only_3pc_wan,
        },
        "plan_kind": (
            "Fixed handwritten original ORQ plans and ORQ-compatible translations of frozen "
            "DuckDB plans. ORQ does not optimize these plans at runtime."
        ),
        "plans": plan_catalog,
        "plans_file": "plans.json",
        "environment_metadata": metadata_text,
        "runs": sorted(runs, key=lambda run: (run["mode"], run["query"], run["variant"])),
        "comparisons": comparisons,
        "failures": failures,
    }


def _format_number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _mean_total_bytes(run: dict[str, Any]) -> int | None:
    means = [entry.get("mean") for entry in run["communicator_bytes_sent"].values()]
    numeric = [value for value in means if value is not None]
    return round(sum(numeric)) if numeric else None


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None or float(denominator) == 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def build_comparisons(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {
        (run["mode"], run["query"], run["variant"]): run
        for run in runs
    }
    modes = sorted({run["mode"] for run in runs})
    comparisons: list[dict[str, Any]] = []
    for mode in modes:
        for query in QUERY_PLANS:
            original = indexed.get((mode, query, "original"))
            derived = indexed.get((mode, query, "duckdb-canonical"))
            if original is None or derived is None:
                continue

            original_mean = original["stage_statistics_seconds"].get("Overall", {}).get("mean")
            derived_mean = derived["stage_statistics_seconds"].get("Overall", {}).get("mean")
            original_bytes = _mean_total_bytes(original)
            derived_bytes = _mean_total_bytes(derived)
            runtime_delta = (
                round(float(derived_mean) - float(original_mean), 6)
                if original_mean is not None and derived_mean is not None
                else None
            )
            runtime_delta_percent = (
                round(runtime_delta / float(original_mean) * 100, 6)
                if runtime_delta is not None and original_mean is not None and float(original_mean) != 0
                else None
            )
            communication_delta = (
                int(derived_bytes - original_bytes)
                if original_bytes is not None and derived_bytes is not None
                else None
            )
            issues = [
                *(f"original: {issue}" for issue in original["issues"]),
                *(f"duckdb-canonical: {issue}" for issue in derived["issues"]),
            ]
            comparisons.append({
                "mode": mode,
                "query": query,
                "status": "success" if not issues else "incomplete",
                "issues": issues,
                "original_mean_seconds": original_mean,
                "duckdb_mean_seconds": derived_mean,
                "runtime_speedup_original_over_duckdb": _ratio(original_mean, derived_mean),
                "runtime_delta_duckdb_minus_original_seconds": runtime_delta,
                "runtime_delta_percent": runtime_delta_percent,
                "original_mean_bytes_sent_per_run": original_bytes,
                "duckdb_mean_bytes_sent_per_run": derived_bytes,
                "communication_ratio_original_over_duckdb": _ratio(
                    original_bytes, derived_bytes
                ),
                "communication_delta_duckdb_minus_original_bytes": communication_delta,
            })
    return comparisons


def _event_text(event: dict[str, Any]) -> str:
    operator = event["operator"]
    if operator == "join":
        return (f"join L={event['left_rows']} R={event['right_rows']} "
                f"keys={event['key_columns']} aggs={event['aggregations']}")
    if operator == "sort":
        return (f"sort {event['algorithm']} rows={event['input_rows']} "
                f"keys={event['key_columns']}")
    if operator == "aggregate":
        return (f"aggregate rows={event['input_rows']} keys={event['key_columns']} "
                f"aggs={event['aggregations']}")
    return f"private division rows={event['input_rows']}"


def render_markdown(data: dict[str, Any]) -> str:
    benchmark = data["benchmark"]
    lines = [
        "# ORQ TPC-H original and DuckDB-derived plan benchmark",
        "",
        (f"TPC-H Q1, Q3, Q5, Q8, and Q9 at scale factor {benchmark['scale_factor']}, "
         f"using {benchmark['threads']} worker threads, {benchmark['repetitions']} repetitions, "
         f"plan set `{benchmark['plan_set']}`, and deterministic data seed "
         f"`{benchmark['data_seed']}`."),
        "",
        "> Both variants are fixed C++ plans. The `duckdb-canonical` variant is a handwritten "
        "ORQ-compatible translation of a frozen DuckDB plan—not an ORQ optimizer result. "
        "DuckDB hash build/probe choices cannot be copied into ORQ's PK/FK join primitive.",
        "",
        "DuckDB estimates in `plans.json` describe standard DBGEN logical rows. ORQ "
        "`[TABLE_*]` observations below describe physical secure-table lengths and must not be "
        "interpreted as equivalent cardinality estimates.",
        "",
        "Query runtime starts at the query's `Start` stopwatch after synthetic database "
        "construction. `QUERY_PROFILE` also disables SQLite validation and result opening.",
        "",
        "## Run summary",
        "",
        "| Mode | Query | Plan | Mean (s) | Median (s) | Min (s) | Max (s) | Stdev (s) | Runs | Mean bytes sent/run | Status |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    if benchmark["wan_mode"] == "userspace":
        lines[2:2] = [
            "> WAN simulation uses no-sudo TCP relays: 6.5 ms per direction and a 12 Gbit/s "
            "application-byte cap per directed party link. This is a qualitative application-traffic "
            "simulation, not kernel packet shaping or a guarantee of 12 Gbit/s throughput. "
            "TCP ACKs and ping are not delayed. Per-party relay byte counters are in results.json.", ""]
    for run in data["runs"]:
        overall = run["stage_statistics_seconds"].get("Overall", {})
        byte_total = _mean_total_bytes(run)
        lines.append(
            f"| {run['mode']} | {run['query'].upper()} | {run['variant']} | "
            f"{_format_number(overall.get('mean'))} | "
            f"{_format_number(overall.get('median'))} | {_format_number(overall.get('min'))} | "
            f"{_format_number(overall.get('max'))} | {_format_number(overall.get('stdev'))} | "
            f"{overall.get('count', 0)} | {byte_total if byte_total is not None else '—'} | "
            f"{run['status']} |"
        )

    if data["comparisons"]:
        lines.extend([
            "",
            "## Original versus DuckDB-derived comparison",
            "",
            "Speedup is `original mean / DuckDB-derived mean`; values above 1 mean the "
            "DuckDB-derived ORQ plan was faster.",
            "",
            "| Mode | Query | Original (s) | DuckDB-derived (s) | Speedup | Delta (s) | Delta (%) | Original bytes | DuckDB bytes | Byte ratio | Status |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ])
        for comparison in data["comparisons"]:
            delta = comparison["runtime_delta_percent"]
            delta_text = "—" if delta is None else f"{delta:+.2f}%"
            lines.append(
                f"| {comparison['mode']} | {comparison['query'].upper()} | "
                f"{_format_number(comparison['original_mean_seconds'])} | "
                f"{_format_number(comparison['duckdb_mean_seconds'])} | "
                f"{_format_number(comparison['runtime_speedup_original_over_duckdb'])} | "
                f"{_format_number(comparison['runtime_delta_duckdb_minus_original_seconds'])} | "
                f"{delta_text} | "
                f"{comparison['original_mean_bytes_sent_per_run'] if comparison['original_mean_bytes_sent_per_run'] is not None else '—'} | "
                f"{comparison['duckdb_mean_bytes_sent_per_run'] if comparison['duckdb_mean_bytes_sent_per_run'] is not None else '—'} | "
                f"{_format_number(comparison['communication_ratio_original_over_duckdb'])} | "
                f"{comparison['status']} |"
            )

    catalog = data["plans"]
    source = catalog["duckdb_source"]
    lines.extend([
        "",
        "## Fixed query plans",
        "",
        (f"Frozen DuckDB source: version `{source['duckdb_version']}`, SF "
         f"`{source['scale_factor']}`, `{source['optimizer_mode']}` mode after `ANALYZE`. "
         "Canonical SQL and complete raw `EXPLAIN (FORMAT JSON)` trees are embedded in "
         "`plans.json`."),
        "",
    ])
    originals = catalog["variants"]["original"]["queries"]
    derived = catalog["variants"]["duckdb-canonical"]["queries"]
    for query in QUERY_PLANS:
        original = originals[query]
        duckdb = derived[query]
        lines.extend([
            f"### {query.upper()}",
            "",
            f"Original ORQ source: `{original['source']}`",
            "",
            "Original ORQ operator order:",
            "",
        ])
        lines.extend(f"{index}. {step}" for index, step in enumerate(original["steps"], 1))
        lines.extend([
            "",
            f"DuckDB association: `{duckdb['association_tree']}`",
            "",
            f"Translation status: `{duckdb['translation_status']}`; target: `{duckdb['orq_target']}`.",
            "",
            duckdb["translation"],
            "",
            "Translated ORQ operator order:",
            "",
        ])
        lines.extend(f"{index}. {step}" for index, step in enumerate(duckdb["orq_steps"], 1))
        lines.append("")

    lines.extend(["## Detailed measurements", ""])
    for run in data["runs"]:
        lines.extend([
            f"### {run['mode']} / {run['query'].upper()} / {run['variant']}", ""
        ])
        if run["issues"]:
            lines.append("Issues: " + "; ".join(run["issues"]))
            lines.append("")
        lines.extend([
            "| Stage | Mean (s) | Median (s) | Min (s) | Max (s) | Stdev (s) | Count |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for label, entry in run["stage_statistics_seconds"].items():
            lines.append(
                f"| {label} | {_format_number(entry.get('mean'))} | "
                f"{_format_number(entry.get('median'))} | {_format_number(entry.get('min'))} | "
                f"{_format_number(entry.get('max'))} | {_format_number(entry.get('stdev'))} | "
                f"{entry.get('count', 0)} |"
            )

        if run["operator_profile_seconds"]:
            lines.extend([
                "",
                "Instrumented table-operator categories:",
                "",
                "| Category | Mean (s) | Median (s) | Min (s) | Max (s) | Count |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ])
            for label, entry in run["operator_profile_seconds"].items():
                lines.append(
                    f"| {label} | {_format_number(entry.get('mean'))} | "
                    f"{_format_number(entry.get('median'))} | {_format_number(entry.get('min'))} | "
                    f"{_format_number(entry.get('max'))} | {entry.get('count', 0)} |"
                )

        if run["network_rtt_ms"]:
            lines.extend([
                "",
                "Network RTT observed by the WAN controller:",
                "",
                "| State | Host | Mean RTT (ms) | Min (ms) | Max (ms) | Samples |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ])
            for section, hosts in run["network_rtt_ms"].items():
                for host, entry in hosts.items():
                    lines.append(
                        f"| {section} | {host} | {_format_number(entry.get('mean'))} | "
                        f"{_format_number(entry.get('min'))} | {_format_number(entry.get('max'))} | "
                        f"{entry.get('count', 0)} |"
                    )

        first_trace = [event for event in run["operator_events"] if event["repetition"] == 0]
        if first_trace:
            lines.extend(["", "Observed operator trace (first repetition):", "", "```text"])
            lines.extend(f"{index:02d}. {_event_text(event)}"
                         for index, event in enumerate(first_trace, 1))
            lines.append("```")

        if run["query_plan_markers"]:
            marker = run["query_plan_markers"][0]
            lines.extend([
                "",
                (f"Compiled plan marker: query `{marker['query']}`, variant "
                 f"`{marker['variant']}`."),
            ])

        artifacts = [
            f"[raw log]({run['raw_log']})" if run["raw_log"] else None,
            f"[profile JSON]({run['profile_json']})" if run["profile_json"] else None,
        ]
        lines.extend(["", "Artifacts: " + ", ".join(item for item in artifacts if item), ""])

    if data["failures"]:
        lines.extend(["## Incomplete runs", ""])
        lines.extend(
            f"- `{item['mode']}/{item['query']}/{item['variant']}`: {item['reason']}"
            for item in data["failures"]
        )
        lines.append("")

    if data["environment_metadata"]:
        lines.extend([
            "## Environment metadata",
            "",
            "<details>",
            "<summary>Captured on the coordinator</summary>",
            "",
            "```text",
            data["environment_metadata"].rstrip(),
            "```",
            "</details>",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scale-factor", required=True, type=float)
    parser.add_argument("--threads", required=True, type=int)
    parser.add_argument("--repetitions", required=True, type=int)
    parser.add_argument("--node-prefix", default="node")
    parser.add_argument("--hosts")
    parser.add_argument("--only-3pc-wan", action="store_true")
    parser.add_argument("--wan-mode", required=True, choices=["simulated", "userspace", "real", "none"])
    parser.add_argument(
        "--plan-set",
        choices=["original", "duckdb-canonical", "both"],
        default="original",
    )
    parser.add_argument("--data-seed", type=int, default=20260818)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.data_seed <= (2**64 - 1):
        parser.error("--data-seed must be an unsigned 64-bit integer")
    if args.only_3pc_wan and args.wan_mode == "none":
        parser.error("--only-3pc-wan requires simulated, userspace or real WAN")
    data = build_report_data(
        output_dir=args.output_dir,
        scale_factor=args.scale_factor,
        threads=args.threads,
        repetitions=args.repetitions,
        node_prefix=args.node_prefix,
        wan_mode=args.wan_mode,
        plan_set=args.plan_set,
        data_seed=args.data_seed,
        hosts=args.hosts.split(",") if args.hosts else None,
        only_3pc_wan=args.only_3pc_wan,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "plans.json").write_text(
        json.dumps(data["plans"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "results.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(render_markdown(data), encoding="utf-8")
    print(f"Wrote {args.output_dir / 'plans.json'}")
    print(f"Wrote {args.output_dir / 'results.json'}")
    print(f"Wrote {args.output_dir / 'report.md'}")
    return 1 if data["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
