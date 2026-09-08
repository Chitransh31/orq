#!/usr/bin/env python3
"""Study how predicate selectivity changes DuckDB's TPC-H query plans.

The experiment uses the standard DuckDB TPC-H generator, independently audits
the selectivity of authored predicates, and compares DuckDB's optimized plan to
an executable, explicitly ordered control plan.  It intentionally treats
operator cardinalities as the primary evidence; timings are diagnostic only.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by real installations
    raise SystemExit(
        "The DuckDB Python package is required. Run: "
        "python3 -m pip install -r requirements.txt"
    ) from exc


SUPPORTED_QUERIES = (1, 3, 5, 8, 9)
SCHEMA_VERSION = 1
FORCED_OPTIMIZERS = ("join_order", "build_side_probe_side")
SCENARIO_FIELDS = (
    "run_id", "query_id", "scenario", "scenario_family", "scenario_order",
    "canonical", "negative_control", "parameters_json", "optimizer_mode",
    "repetition", "result_rows", "result_checksum", "canonical_template_match",
    "association_fingerprint", "ordered_fingerprint", "orientation_fingerprint",
    "static_analyzed_fingerprint_match", "max_join_actual_rows",
    "profile_latency_seconds", "profile_wall_seconds", "raw_dir",
)
OPERATOR_FIELDS = (
    "run_id", "query_id", "scenario", "mode", "repetition", "node_id",
    "parent_id", "depth", "operator_name", "operator_type", "relation_set",
    "estimated_cardinality", "actual_cardinality", "input_cardinality",
    "unary_ratio", "relative_error", "q_error", "smoothed_q_error",
    "operator_timing_seconds", "operator_rows_scanned", "extra_info_json",
)
JOIN_FIELDS = (
    "run_id", "query_id", "scenario", "mode", "node_id", "operator_name",
    "join_type", "conditions", "left_relations", "right_relations",
    "output_relations", "left_estimated_cardinality",
    "right_estimated_cardinality", "estimated_cardinality",
    "left_actual_cardinality", "right_actual_cardinality", "actual_cardinality",
    "output_per_left", "output_per_right", "pair_selectivity",
    "association_node", "ordered_node",
)
AUDIT_FIELDS = (
    "scenario_id", "query_id", "audit_id", "audit_kind", "scope",
    "predicate_or_formula", "numerator", "denominator", "value", "unit", "notes",
)
TRANSITION_FIELDS = (
    "query_id", "scenario_family", "parameter_name", "parameter_type",
    "low_value", "high_value", "low_realized_selectivity_json",
    "high_realized_selectivity_json", "low_association_fingerprint",
    "high_association_fingerprint", "low_ordered_fingerprint",
    "high_ordered_fingerprint", "low_orientation_fingerprint",
    "high_orientation_fingerprint", "low_metadata_fingerprint",
    "high_metadata_fingerprint", "association_changed", "orientation_changed",
    "ordered_detail_changed", "change_kind", "resolution", "probe_count",
    "low_raw_dir", "high_raw_dir", "search_scope",
)


@dataclass(frozen=True)
class Scenario:
    query_id: int
    name: str
    family: str
    order: int
    description: str
    parameters: Mapping[str, Any]
    canonical: bool = False
    negative_control: bool = False


def safe_ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    """Return a ratio without silently converting an undefined ratio to zero."""

    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def cardinality_errors(estimated: int | float | None, actual: int | float | None) -> dict[str, float | None]:
    """Return signed relative error and conventional/smoothed q-errors."""

    if estimated is None or actual is None:
        return {"relative_error": None, "q_error": None, "smoothed_q_error": None}

    estimate = float(estimated)
    observed = float(actual)
    if observed == 0:
        relative_error = None
    else:
        relative_error = (estimate - observed) / observed

    if estimate == 0 and observed == 0:
        q_error = 1.0
    elif estimate == 0 or observed == 0:
        q_error = math.inf
    else:
        q_error = max(estimate / observed, observed / estimate)

    smoothed_q_error = max(
        (estimate + 1.0) / (observed + 1.0),
        (observed + 1.0) / (estimate + 1.0),
    )
    return {
        "relative_error": relative_error,
        "q_error": q_error,
        "smoothed_q_error": smoothed_q_error,
    }


def parse_json_explain(value: str | bytes | dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any]:
    """Parse the value returned by DuckDB's JSON EXPLAIN formats."""

    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    parsed = json.loads(value)
    if not isinstance(parsed, (dict, list)):
        raise ValueError("DuckDB EXPLAIN JSON did not contain an object or array")
    return parsed


def _operator_name(node: Mapping[str, Any]) -> str:
    return str(node.get("operator_name") or node.get("name") or node.get("operator_type") or "UNKNOWN")


def _children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    children = node.get("children") or []
    return [child for child in children if isinstance(child, Mapping)]


def _parse_cardinality(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?[0-9][0-9,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def _estimated_cardinality(node: Mapping[str, Any]) -> int | None:
    extra = node.get("extra_info") or {}
    if not isinstance(extra, Mapping):
        return None
    return _parse_cardinality(extra.get("Estimated Cardinality"))


def _actual_cardinality(node: Mapping[str, Any]) -> int | None:
    return _parse_cardinality(node.get("operator_cardinality"))


def _is_join(node: Mapping[str, Any]) -> bool:
    name = _operator_name(node).upper().strip()
    return name.endswith("JOIN") or name == "CROSS_PRODUCT"


def _projection_names(extra: Mapping[str, Any]) -> set[str]:
    value = extra.get("Projections", [])
    if isinstance(value, str):
        value = [value]
    return {str(item).lower() for item in value}


def _scan_relation(node: Mapping[str, Any], distinguish_nation_roles: bool = False) -> str | None:
    name = _operator_name(node).upper()
    if "SCAN" not in name:
        return None
    extra = node.get("extra_info") or {}
    if not isinstance(extra, Mapping):
        return None
    table = extra.get("Table")
    if not table:
        return None
    relation = str(table).split(".")[-1].strip().lower()

    # DuckDB's physical JSON omits SQL aliases.  Repeated nation scans can be
    # distinguished neutrally by the columns they project.
    if relation == "nation" and distinguish_nation_roles:
        projections = _projection_names(extra)
        if "n_regionkey" in projections and "n_name" not in projections:
            return "nation_region"
        if "n_name" in projections and "n_regionkey" not in projections:
            return "nation_name"
    return relation


def _relation_leaves(
    node: Mapping[str, Any], distinguish_nation_roles: bool = False
) -> tuple[str, ...]:
    relation = _scan_relation(node, distinguish_nation_roles)
    if relation:
        return (relation,)
    leaves: list[str] = []
    for child in _children(node):
        leaves.extend(_relation_leaves(child, distinguish_nation_roles))
    return tuple(sorted(leaves))


def _nation_scan_count(root: dict[str, Any] | list[Any]) -> int:
    def count(node: Mapping[str, Any]) -> int:
        relation = _scan_relation(node, distinguish_nation_roles=False)
        return (1 if relation == "nation" else 0) + sum(count(child) for child in _children(node))

    return sum(count(node) for node in _root_nodes(root))


def _normalize_conditions(extra: Mapping[str, Any]) -> str:
    conditions = extra.get("Conditions", "")
    if isinstance(conditions, Sequence) and not isinstance(conditions, str):
        parts = [str(item) for item in conditions]
    else:
        parts = str(conditions).splitlines()
    normalized: list[str] = []
    for part in parts:
        condition = re.sub(r"\s+", " ", str(part)).strip().lower()
        if not condition:
            continue
        equality = re.fullmatch(r"(.+?)\s*=\s*(.+)", condition)
        if equality:
            condition = " = ".join(sorted((equality.group(1).strip(), equality.group(2).strip())))
        normalized.append(condition)
    return "&".join(sorted(normalized))


def _fingerprint_node(
    node: Mapping[str, Any],
    ordered: bool,
    distinguish_nation_roles: bool = False,
    include_join_metadata: bool = True,
) -> str:
    relation = _scan_relation(node, distinguish_nation_roles)
    children = _children(node)
    if relation:
        return relation
    if _is_join(node) and len(children) >= 2:
        child_fingerprints = [
            _fingerprint_node(child, ordered, distinguish_nation_roles, include_join_metadata)
            for child in children
        ]
        if not ordered:
            child_fingerprints.sort()
        if not include_join_metadata:
            return f"join({','.join(child_fingerprints)})"
        extra = node.get("extra_info") or {}
        join_type = str(extra.get("Join Type", _operator_name(node))).strip().lower()
        conditions = _normalize_conditions(extra) if isinstance(extra, Mapping) else ""
        return f"join[{join_type};{conditions}]({','.join(child_fingerprints)})"
    if len(children) == 1:
        return _fingerprint_node(
            children[0], ordered, distinguish_nation_roles, include_join_metadata
        )
    if len(children) > 1:
        values = [
            _fingerprint_node(child, ordered, distinguish_nation_roles, include_join_metadata)
            for child in children
        ]
        if not ordered:
            values.sort()
        return f"branch({','.join(values)})"
    return _operator_name(node).strip().lower()


def _root_nodes(root: dict[str, Any] | list[Any]) -> list[Mapping[str, Any]]:
    if isinstance(root, list):
        return [node for node in root if isinstance(node, Mapping)]
    return [root]


def plan_fingerprints(root: dict[str, Any] | list[Any]) -> tuple[str, str]:
    """Return association-only and child-orientation-preserving signatures."""

    roots = _root_nodes(root)
    distinguish_nation_roles = _nation_scan_count(root) > 1
    association = [
        _fingerprint_node(
            node,
            ordered=False,
            distinguish_nation_roles=distinguish_nation_roles,
            include_join_metadata=False,
        )
        for node in roots
    ]
    oriented = [
        _fingerprint_node(node, ordered=True, distinguish_nation_roles=distinguish_nation_roles)
        for node in roots
    ]
    return ("|".join(sorted(association)), "|".join(oriented))


def plan_orientation_fingerprint(root: dict[str, Any] | list[Any]) -> str:
    """Return child orientation without join type/condition details."""

    roots = _root_nodes(root)
    distinguish_nation_roles = _nation_scan_count(root) > 1
    values = [
        _fingerprint_node(
            node,
            ordered=True,
            distinguish_nation_roles=distinguish_nation_roles,
            include_join_metadata=False,
        )
        for node in roots
    ]
    return "|".join(values)


def plan_metadata_fingerprint(root: dict[str, Any] | list[Any]) -> str:
    """Return join type/conditions and association while ignoring child orientation."""

    roots = _root_nodes(root)
    distinguish_nation_roles = _nation_scan_count(root) > 1
    values = [
        _fingerprint_node(
            node,
            ordered=False,
            distinguish_nation_roles=distinguish_nation_roles,
            include_join_metadata=True,
        )
        for node in roots
    ]
    return "|".join(sorted(values))


def classify_plan_change(
    low_association: str,
    high_association: str,
    low_orientation: str,
    high_orientation: str,
    low_metadata: str,
    high_metadata: str,
) -> dict[str, bool | str]:
    """Classify association, child-orientation, and join-metadata changes separately."""

    association_changed = low_association != high_association
    # Child orientation and metadata placement are comparable only when the
    # same relation sets associate at every join node.
    orientation_changed = (
        not association_changed and low_orientation != high_orientation
    )
    ordered_detail_changed = (
        not association_changed and low_metadata != high_metadata
    )
    if association_changed:
        kind = "association"
    elif orientation_changed and ordered_detail_changed:
        kind = "orientation_and_ordered_detail"
    elif orientation_changed:
        kind = "orientation"
    elif ordered_detail_changed:
        kind = "ordered_detail"
    else:
        kind = "none"
    return {
        "association_changed": association_changed,
        "orientation_changed": orientation_changed,
        "ordered_detail_changed": ordered_detail_changed,
        "change_kind": kind,
    }


def flatten_plan(
    root: dict[str, Any] | list[Any],
    query_id: int | str,
    scenario: str,
    mode: str,
) -> list[dict[str, Any]]:
    """Flatten either DuckDB JSON plan shape into operator-table rows."""

    rows: list[dict[str, Any]] = []
    distinguish_nation_roles = _nation_scan_count(root) > 1

    def visit(
        node: Mapping[str, Any], path: tuple[int, ...], parent: str | None, depth: int
    ) -> None:
        node_id = ".".join(str(item) for item in path)
        children = _children(node)
        operator_name = _operator_name(node)
        emit = operator_name.upper() not in ("UNKNOWN", "EXPLAIN_ANALYZE")
        estimated = _estimated_cardinality(node)
        actual = _actual_cardinality(node)
        child_actual = _actual_cardinality(children[0]) if len(children) == 1 else None
        errors = cardinality_errors(estimated, actual)
        extra = node.get("extra_info") or {}
        if emit:
            rows.append(
                {
                    "query_id": query_id,
                    "scenario": scenario,
                    "mode": mode,
                    "node_id": node_id,
                    "parent_id": parent,
                    "depth": depth,
                    "operator_name": operator_name,
                    "operator_type": node.get("operator_type"),
                    "relation_set": "|".join(_relation_leaves(node, distinguish_nation_roles)),
                    "estimated_cardinality": estimated,
                    "actual_cardinality": actual,
                    "input_cardinality": child_actual,
                    "unary_ratio": safe_ratio(actual, child_actual) if len(children) == 1 else None,
                    "relative_error": errors["relative_error"],
                    "q_error": errors["q_error"],
                    "smoothed_q_error": errors["smoothed_q_error"],
                    "operator_timing_seconds": node.get("operator_timing"),
                    "operator_rows_scanned": node.get("operator_rows_scanned"),
                    "extra_info_json": json.dumps(extra, sort_keys=True, default=str),
                }
            )
        for index, child in enumerate(children):
            visit(child, (*path, index), node_id if emit else parent, depth + (1 if emit else 0))

    for index, node in enumerate(_root_nodes(root)):
        visit(node, (index,), None, 0)
    return rows


def _set_disabled_optimizers(connection: duckdb.DuckDBPyConnection, value: str) -> None:
    escaped = value.replace("'", "''")
    connection.execute(f"SET disabled_optimizers = '{escaped}'")


@contextlib.contextmanager
def optimizer_mode(connection: duckdb.DuckDBPyConnection, mode: str) -> Iterator[None]:
    """Temporarily select normal or forced SQL-order join optimization."""

    previous = str(
        connection.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    )
    if mode == "optimized":
        effective = ""
    elif mode == "forced":
        effective = ",".join(FORCED_OPTIMIZERS)
    else:
        raise ValueError(f"unknown optimizer mode: {mode}")
    try:
        _set_disabled_optimizers(connection, effective)
        yield
    finally:
        _set_disabled_optimizers(connection, previous)


def _date_literal(value: str) -> str:
    date.fromisoformat(value)
    return f"DATE '{value}'"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def query_sql(scenario: Scenario) -> str:
    """Render a scenario with literals so DuckDB can estimate concrete values."""

    p = scenario.parameters
    if scenario.query_id == 1:
        return f"""
SELECT
    l_returnflag,
    l_linestatus,
    sum(l_quantity) AS sum_qty,
    sum(l_extendedprice) AS sum_base_price,
    sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    avg(l_quantity) AS avg_qty,
    avg(l_extendedprice) AS avg_price,
    avg(l_discount) AS avg_disc,
    count(*) AS count_order
FROM lineitem AS l
WHERE l.l_shipdate <= {_date_literal(str(p['cutoff']))}
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
""".strip()

    if scenario.query_id == 3:
        cutoff = _date_literal(str(p["cutoff"]))
        return f"""
SELECT
    l.l_orderkey,
    sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
    o.o_orderdate,
    o.o_shippriority
FROM customer AS c
JOIN orders AS o ON c.c_custkey = o.o_custkey
JOIN lineitem AS l ON o.o_orderkey = l.l_orderkey
WHERE c.c_mktsegment = {_sql_string(str(p['segment']))}
  AND o.o_orderdate < {cutoff}
  AND l.l_shipdate > {cutoff}
GROUP BY l.l_orderkey, o.o_orderdate, o.o_shippriority
ORDER BY revenue DESC, o.o_orderdate
LIMIT 10
""".strip()

    if scenario.query_id == 5:
        return f"""
SELECT
    n.n_name,
    sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue
FROM region AS r
JOIN nation AS n ON r.r_regionkey = n.n_regionkey
JOIN customer AS c ON n.n_nationkey = c.c_nationkey
JOIN orders AS o ON c.c_custkey = o.o_custkey
JOIN lineitem AS l ON o.o_orderkey = l.l_orderkey
JOIN supplier AS s
  ON l.l_suppkey = s.s_suppkey
 AND n.n_nationkey = s.s_nationkey
WHERE r.r_name = {_sql_string(str(p['region']))}
  AND o.o_orderdate >= {_date_literal(str(p['start']))}
  AND o.o_orderdate < {_date_literal(str(p['end']))}
GROUP BY n.n_name
ORDER BY revenue DESC
""".strip()

    if scenario.query_id == 8:
        type_predicate = str(p["type_predicate"])
        return f"""
SELECT
    o_year,
    sum(CASE WHEN nation = {_sql_string(str(p['case_nation']))}
             THEN volume ELSE 0 END) / sum(volume) AS mkt_share
FROM (
    SELECT
        extract(year FROM o.o_orderdate) AS o_year,
        l.l_extendedprice * (1 - l.l_discount) AS volume,
        n2.n_name AS nation
    FROM part AS p
    JOIN lineitem AS l ON p.p_partkey = l.l_partkey
    JOIN orders AS o ON l.l_orderkey = o.o_orderkey
    JOIN customer AS c ON o.o_custkey = c.c_custkey
    JOIN nation AS n1 ON c.c_nationkey = n1.n_nationkey
    JOIN region AS r ON n1.n_regionkey = r.r_regionkey
    JOIN supplier AS s ON l.l_suppkey = s.s_suppkey
    JOIN nation AS n2 ON s.s_nationkey = n2.n_nationkey
    WHERE r.r_name = {_sql_string(str(p['region']))}
      AND o.o_orderdate BETWEEN {_date_literal(str(p['start']))}
                            AND {_date_literal(str(p['end']))}
      AND ({type_predicate})
) AS all_nations
GROUP BY o_year
ORDER BY o_year
""".strip()

    if scenario.query_id == 9:
        return f"""
SELECT
    nation,
    o_year,
    sum(amount) AS sum_profit
FROM (
    SELECT
        n.n_name AS nation,
        extract(year FROM o.o_orderdate) AS o_year,
        l.l_extendedprice * (1 - l.l_discount)
            - ps.ps_supplycost * l.l_quantity AS amount
    FROM nation AS n
    JOIN supplier AS s ON n.n_nationkey = s.s_nationkey
    JOIN lineitem AS l ON s.s_suppkey = l.l_suppkey
    JOIN part AS p ON l.l_partkey = p.p_partkey
    JOIN orders AS o ON l.l_orderkey = o.o_orderkey
    JOIN partsupp AS ps
      ON ps.ps_partkey = l.l_partkey
     AND ps.ps_suppkey = l.l_suppkey
    WHERE {p['part_predicate']}
) AS profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC
""".strip()

    raise ValueError(f"unsupported query: {scenario.query_id}")


def build_scenarios(
    connection: duckdb.DuckDBPyConnection, query_ids: Sequence[int]
) -> list[Scenario]:
    scenarios: list[Scenario] = []
    if 1 in query_ids:
        for order, (name, cutoff, canonical) in enumerate(
            (
                ("shipdate_p05", "1992-06-30", False),
                ("shipdate_p50", "1995-06-17", False),
                ("shipdate_p95", "1998-06-04", False),
                ("canonical", "1998-09-02", True),
            )
        ):
            scenarios.append(
                Scenario(1, name, "shipdate_cutoff", order, f"l_shipdate <= {cutoff}", {"cutoff": cutoff}, canonical)
            )

    if 3 in query_ids:
        for order, (name, cutoff, canonical) in enumerate(
            (
                ("date_early", "1993-01-01", False),
                ("canonical", "1995-03-15", True),
                ("date_late", "1998-07-01", False),
            )
        ):
            scenarios.append(
                Scenario(
                    3,
                    name,
                    "shared_date_cutoff",
                    order,
                    f"orders before and shipments after {cutoff}",
                    {"cutoff": cutoff, "segment": "BUILDING"},
                    canonical,
                )
            )

    if 5 in query_ids:
        for order, (name, end, canonical) in enumerate(
            (
                ("window_one_month", "1994-02-01", False),
                ("canonical", "1995-01-01", True),
                ("window_wide", "1998-08-03", False),
            )
        ):
            scenarios.append(
                Scenario(
                    5,
                    name,
                    "date_window_end",
                    order,
                    f"order window [1994-01-01, {end})",
                    {"start": "1994-01-01", "end": end, "region": "ASIA"},
                    canonical,
                )
            )

    if 8 in query_ids:
        control_nation_row = connection.execute(
            """
SELECT n2.n_name
FROM part p
JOIN lineitem l ON p.p_partkey=l.l_partkey
JOIN orders o ON l.l_orderkey=o.o_orderkey
JOIN customer c ON o.o_custkey=c.c_custkey
JOIN nation n1 ON c.c_nationkey=n1.n_nationkey
JOIN region r ON n1.n_regionkey=r.r_regionkey
JOIN supplier s ON l.l_suppkey=s.s_suppkey
JOIN nation n2 ON s.s_nationkey=n2.n_nationkey
WHERE r.r_name='AMERICA'
  AND n2.n_regionkey=r.r_regionkey
  AND o.o_orderdate BETWEEN DATE '1995-01-01' AND DATE '1996-12-31'
  AND p.p_type='ECONOMY ANODIZED STEEL'
  AND n2.n_name <> 'BRAZIL'
GROUP BY n2.n_name
ORDER BY sum(l.l_extendedprice * (1-l.l_discount)) DESC
LIMIT 1
"""
        ).fetchone()
        control_nation = str(control_nation_row[0]) if control_nation_row else "CANADA"
        q8_common = {
            "case_nation": "BRAZIL",
            "region": "AMERICA",
            "start": "1995-01-01",
            "end": "1996-12-31",
        }
        scenarios.extend(
            (
                Scenario(
                    8,
                    "canonical",
                    "part_type_breadth",
                    0,
                    "exact canonical part type",
                    {**q8_common, "type_predicate": "p.p_type = 'ECONOMY ANODIZED STEEL'"},
                    True,
                ),
                Scenario(
                    8,
                    "type_family",
                    "part_type_breadth",
                    1,
                    "all ECONOMY part types",
                    {**q8_common, "type_predicate": "p.p_type LIKE 'ECONOMY %'"},
                ),
                Scenario(
                    8,
                    "type_unrestricted",
                    "part_type_breadth",
                    2,
                    "no part-type restriction",
                    {**q8_common, "type_predicate": "TRUE"},
                ),
                Scenario(
                    8,
                    "case_nation_control",
                    "case_nation_control",
                    0,
                    "CASE nation changed without changing row predicates",
                    {
                        **q8_common,
                        "case_nation": control_nation,
                        "control_has_contributing_rows": control_nation_row is not None,
                        "type_predicate": "p.p_type = 'ECONOMY ANODIZED STEEL'",
                    },
                    negative_control=True,
                ),
            )
        )

    if 9 in query_ids:
        scenarios.append(
            Scenario(
                9,
                "canonical",
                "name_like",
                0,
                "canonical color substring",
                {"part_predicate": "p.p_name LIKE '%green%'", "fraction": None, "threshold": None},
                True,
            )
        )
        part_count = int(connection.execute("SELECT count(*) FROM part").fetchone()[0])
        if part_count <= 0:
            raise RuntimeError("Q9 numeric controls require DBGEN to produce at least one part")
        for order, (name, fraction) in enumerate(
            (("partkey_p00025", 0.0025), ("partkey_p05", 0.05), ("partkey_p80", 0.80))
        ):
            target_rank = min(part_count, max(1, math.floor(part_count * fraction)))
            threshold = int(
                connection.execute(
                    "SELECT p_partkey FROM part ORDER BY p_partkey LIMIT 1 OFFSET ?",
                    [target_rank - 1],
                ).fetchone()[0]
            )
            scenarios.append(
                Scenario(
                    9,
                    name,
                    "partkey_fraction",
                    order,
                    f"lowest {fraction:.4%} of part keys",
                    {
                        "part_predicate": f"p.p_partkey <= {threshold}",
                        "fraction": fraction,
                        "threshold": threshold,
                    },
                )
            )
    return scenarios


def _count(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    value = connection.execute(sql).fetchone()[0]
    return int(value)


def _audit_row(
    scenario: Scenario,
    audit_id: str,
    audit_kind: str,
    scope: str,
    expression: str,
    numerator: int | float,
    denominator: int | float | None = None,
    *,
    value: int | float | None = None,
    unit: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    if value is None:
        value = safe_ratio(numerator, denominator) if denominator is not None else numerator
    if unit is None:
        unit = "ratio" if denominator is not None else "rows"
    return {
        "scenario_id": f"q{scenario.query_id:02d}.{scenario.name}",
        "query_id": scenario.query_id,
        "audit_id": audit_id,
        "audit_kind": audit_kind,
        "scope": scope,
        "predicate_or_formula": expression,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": unit,
        "notes": notes,
    }


def audit_scenario(
    connection: duckdb.DuckDBPyConnection, scenario: Scenario
) -> list[dict[str, Any]]:
    """Measure authored filters and logical intermediates independently."""

    p = scenario.parameters
    rows: list[dict[str, Any]] = []

    if scenario.query_id == 1:
        predicate = f"l_shipdate <= {_date_literal(str(p['cutoff']))}"
        total, selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {predicate}) FROM lineitem"
        ).fetchone()
        rows.append(
            _audit_row(scenario, "lineitem_shipdate", "base_predicate", "lineitem", predicate, selected, total)
        )
        rows.append(
            _audit_row(
                scenario,
                "result_groups",
                "result",
                "query",
                "count output groups",
                _count(connection, f"SELECT count(*) FROM ({query_sql(scenario)}) AS q"),
            )
        )
        return rows

    if scenario.query_id == 3:
        cutoff = _date_literal(str(p["cutoff"]))
        segment = _sql_string(str(p["segment"]))
        base_specs = (
            ("customer_segment", "customer", f"c_mktsegment = {segment}"),
            ("orders_date", "orders", f"o_orderdate < {cutoff}"),
            ("lineitem_shipdate", "lineitem", f"l_shipdate > {cutoff}"),
        )
        base_values: dict[str, tuple[int, int]] = {}
        for audit_id, table, predicate in base_specs:
            total, selected = connection.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {predicate}) FROM {table}"
            ).fetchone()
            base_values[audit_id] = (int(total), int(selected))
            rows.append(
                _audit_row(scenario, audit_id, "base_predicate", table, predicate, selected, total)
            )

        co = _count(
            connection,
            f"""
SELECT count(*)
FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey
WHERE c.c_mktsegment = {segment} AND o.o_orderdate < {cutoff}
""",
        )
        ol_stats = connection.execute(
            f"""
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE o.o_orderdate < {cutoff}) AS order_only,
    count(*) FILTER (WHERE l.l_shipdate > {cutoff}) AS ship_only,
    count(*) FILTER (
        WHERE o.o_orderdate < {cutoff} AND l.l_shipdate > {cutoff}
    ) AS both
FROM orders o JOIN lineitem l ON o.o_orderkey = l.l_orderkey
"""
        ).fetchone()
        ol_total, order_weighted, ship_only, ol_both = map(int, ol_stats)
        full = _count(
            connection,
            f"""
SELECT count(*)
FROM customer c
JOIN orders o ON c.c_custkey = o.o_custkey
JOIN lineitem l ON o.o_orderkey = l.l_orderkey
WHERE c.c_mktsegment = {segment}
  AND o.o_orderdate < {cutoff}
  AND l.l_shipdate > {cutoff}
""",
        )
        rows.extend(
            (
                _audit_row(scenario, "customer_orders", "intermediate", "customer_join_orders", "filtered C join filtered O", co),
                _audit_row(scenario, "orders_lineitem", "intermediate", "orders_join_lineitem", "both date predicates", ol_both),
                _audit_row(scenario, "full_preaggregate", "intermediate", "customer_orders_lineitem", "all Q3 predicates", full),
            )
        )
        independent_expected = safe_ratio(order_weighted * ship_only, ol_total)
        lift = safe_ratio(ol_both, independent_expected)
        rows.append(
            _audit_row(
                scenario,
                "date_correlation_lift",
                "correlation",
                "orders_join_lineitem",
                "joint / (lineitem-weighted order marginal * ship marginal)",
                ol_both,
                independent_expected,
                value=lift,
                unit="factor",
                notes=(
                    f"OL population={ol_total}; order marginal rows={order_weighted}; "
                    f"ship marginal rows={ship_only}"
                ),
            )
        )
        return rows

    if scenario.query_id == 5:
        region = _sql_string(str(p["region"]))
        start = _date_literal(str(p["start"]))
        end = _date_literal(str(p["end"]))
        order_predicate = f"o_orderdate >= {start} AND o_orderdate < {end}"
        order_total, order_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {order_predicate}) FROM orders"
        ).fetchone()
        region_total, region_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE r_name = {region}) FROM region"
        ).fetchone()
        rows.extend(
            (
                _audit_row(scenario, "region_name", "base_predicate", "region", f"r_name = {region}", region_selected, region_total),
                _audit_row(scenario, "orders_date", "base_predicate", "orders", order_predicate, order_selected, order_total),
            )
        )
        audit_sql = {
            "selected_nations": f"""
SELECT count(*) FROM region r JOIN nation n ON r.r_regionkey=n.n_regionkey
WHERE r.r_name={region}
""",
            "selected_customers": f"""
SELECT count(*) FROM region r
JOIN nation n ON r.r_regionkey=n.n_regionkey
JOIN customer c ON n.n_nationkey=c.c_nationkey
WHERE r.r_name={region}
""",
            "selected_suppliers": f"""
SELECT count(*) FROM region r
JOIN nation n ON r.r_regionkey=n.n_regionkey
JOIN supplier s ON n.n_nationkey=s.s_nationkey
WHERE r.r_name={region}
""",
            "regional_date_orders": f"""
SELECT count(*) FROM region r
JOIN nation n ON r.r_regionkey=n.n_regionkey
JOIN customer c ON n.n_nationkey=c.c_nationkey
JOIN orders o ON c.c_custkey=o.o_custkey
WHERE r.r_name={region} AND {order_predicate}
""",
            "date_order_lineitems": f"""
SELECT count(*) FROM orders o JOIN lineitem l ON o.o_orderkey=l.l_orderkey
WHERE {order_predicate}
""",
            "full_preaggregate": f"""
SELECT count(*) FROM region r
JOIN nation n ON r.r_regionkey=n.n_regionkey
JOIN customer c ON n.n_nationkey=c.c_nationkey
JOIN orders o ON c.c_custkey=o.o_custkey
JOIN lineitem l ON o.o_orderkey=l.l_orderkey
JOIN supplier s ON l.l_suppkey=s.s_suppkey AND n.n_nationkey=s.s_nationkey
WHERE r.r_name={region} AND {order_predicate}
""",
        }
        for audit_id, sql in audit_sql.items():
            rows.append(
                _audit_row(scenario, audit_id, "intermediate", "q5", audit_id.replace("_", " "), _count(connection, sql))
            )
        lineitem_total = _count(connection, "SELECT count(*) FROM lineitem")
        final_rows = int(next(row["numerator"] for row in rows if row["audit_id"] == "full_preaggregate"))
        rows.append(
            _audit_row(scenario, "final_lineitem_fraction", "derived", "q5", "full preaggregate / lineitem", final_rows, lineitem_total)
        )
        return rows

    if scenario.query_id == 8:
        type_predicate = str(p["type_predicate"])
        region = _sql_string(str(p["region"]))
        start = _date_literal(str(p["start"]))
        end = _date_literal(str(p["end"]))
        order_predicate = f"o_orderdate BETWEEN {start} AND {end}"
        part_total, part_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {type_predicate}) FROM part p"
        ).fetchone()
        order_total, order_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {order_predicate}) FROM orders o"
        ).fetchone()
        region_total, region_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE r_name = {region}) FROM region"
        ).fetchone()
        rows.extend(
            (
                _audit_row(scenario, "part_type", "base_predicate", "part", type_predicate, part_selected, part_total),
                _audit_row(scenario, "orders_date", "base_predicate", "orders", order_predicate, order_selected, order_total),
                _audit_row(scenario, "region_name", "base_predicate", "region", f"r_name = {region}", region_selected, region_total),
            )
        )
        regional_customers = _count(
            connection,
            f"""
SELECT count(*) FROM customer c
JOIN nation n1 ON c.c_nationkey=n1.n_nationkey
JOIN region r ON n1.n_regionkey=r.r_regionkey
WHERE r.r_name={region}
""",
        )
        customer_total = _count(connection, "SELECT count(*) FROM customer")
        rows.append(
            _audit_row(scenario, "regional_customers", "base_path", "customer_nation_region", f"r_name = {region}", regional_customers, customer_total)
        )
        part_lineitems = _count(
            connection,
            f"""
SELECT count(*) FROM part p JOIN lineitem l ON p.p_partkey=l.l_partkey
WHERE {type_predicate}
""",
        )
        final_sql = f"""
SELECT count(*) FROM part p
JOIN lineitem l ON p.p_partkey=l.l_partkey
JOIN orders o ON l.l_orderkey=o.o_orderkey
JOIN customer c ON o.o_custkey=c.c_custkey
JOIN nation n1 ON c.c_nationkey=n1.n_nationkey
JOIN region r ON n1.n_regionkey=r.r_regionkey
JOIN supplier s ON l.l_suppkey=s.s_suppkey
JOIN nation n2 ON s.s_nationkey=n2.n_nationkey
WHERE r.r_name={region} AND {order_predicate} AND ({type_predicate})
"""
        final_rows = _count(connection, final_sql)
        rows.extend(
            (
                _audit_row(scenario, "part_lineitems", "intermediate", "part_join_lineitem", "filtered parts join lineitem", part_lineitems),
                _audit_row(scenario, "full_preaggregate", "intermediate", "q8", "all row-reducing Q8 predicates", final_rows),
            )
        )
        return rows

    if scenario.query_id == 9:
        predicate = str(p["part_predicate"])
        part_total, part_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {predicate}) FROM part p"
        ).fetchone()
        part_lineitems = _count(
            connection,
            f"""
SELECT count(*) FROM part p JOIN lineitem l ON p.p_partkey=l.l_partkey
WHERE {predicate}
""",
        )
        partsupp_join_rows = _count(
            connection,
            f"""
SELECT count(*) FROM part p
JOIN lineitem l ON p.p_partkey=l.l_partkey
JOIN partsupp ps
  ON ps.ps_partkey=l.l_partkey AND ps.ps_suppkey=l.l_suppkey
WHERE {predicate}
""",
        )
        matched_lineitems = _count(
            connection,
            f"""
SELECT count(*) FROM part p
JOIN lineitem l ON p.p_partkey=l.l_partkey
WHERE {predicate}
  AND EXISTS (
      SELECT 1 FROM partsupp ps
      WHERE ps.ps_partkey=l.l_partkey AND ps.ps_suppkey=l.l_suppkey
  )
""",
        )
        partsupp_total, partsupp_distinct = connection.execute(
            """
SELECT count(*), count(DISTINCT (ps_partkey, ps_suppkey))
FROM partsupp
"""
        ).fetchone()
        full = _count(
            connection,
            f"SELECT count(*) FROM ({_q9_preaggregate_sql(predicate)}) AS q",
        )
        rows.extend(
            (
                _audit_row(scenario, "part_filter", "base_predicate", "part", predicate, part_selected, part_total),
                _audit_row(scenario, "part_lineitems", "intermediate", "part_join_lineitem", "filtered parts join lineitem", part_lineitems),
                _audit_row(scenario, "partsupp_join_rows", "intermediate", "part_lineitem_partsupp", "rows after composite partsupp join", partsupp_join_rows),
                _audit_row(scenario, "partsupp_matched_lineitems", "intermediate", "part_lineitem_partsupp", "part-lineitems having at least one composite match", matched_lineitems),
                _audit_row(scenario, "partsupp_retention", "derived", "part_lineitem_partsupp", "matched part-lineitems / all part-lineitems", matched_lineitems, part_lineitems),
                _audit_row(scenario, "partsupp_match_fanout", "derived", "part_lineitem_partsupp", "join rows / matched part-lineitems", partsupp_join_rows, matched_lineitems),
                _audit_row(scenario, "partsupp_key_uniqueness", "integrity", "partsupp", "distinct composite keys / partsupp rows", partsupp_distinct, partsupp_total),
                _audit_row(scenario, "full_preaggregate", "intermediate", "q9", "all Q9 joins before aggregation", full),
            )
        )
        return rows

    raise ValueError(f"unsupported query: {scenario.query_id}")


def _q9_preaggregate_sql(part_predicate: str) -> str:
    return f"""
SELECT n.n_name, o.o_orderdate, l.l_extendedprice, l.l_discount,
       ps.ps_supplycost, l.l_quantity
FROM nation n
JOIN supplier s ON n.n_nationkey=s.s_nationkey
JOIN lineitem l ON s.s_suppkey=l.l_suppkey
JOIN part p ON l.l_partkey=p.p_partkey
JOIN orders o ON l.l_orderkey=o.o_orderkey
JOIN partsupp ps
  ON ps.ps_partkey=l.l_partkey AND ps.ps_suppkey=l.l_suppkey
WHERE {part_predicate}
""".strip()


def _explain_rows(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    analyze: bool,
    json_format: bool,
) -> list[tuple[str, str]]:
    if json_format:
        options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
        statement = f"EXPLAIN ({options}) {sql}"
    else:
        statement = f"EXPLAIN ANALYZE {sql}" if analyze else f"EXPLAIN {sql}"
    return [(str(key), str(value)) for key, value in connection.execute(statement).fetchall()]


def explain_json(
    connection: duckdb.DuckDBPyConnection, sql: str, *, analyze: bool
) -> dict[str, Any] | list[Any]:
    rows = dict(_explain_rows(connection, sql, analyze=analyze, json_format=True))
    expected = "analyzed_plan" if analyze else "physical_plan"
    if expected not in rows:
        available = ", ".join(sorted(rows))
        raise RuntimeError(f"DuckDB did not return {expected}; available keys: {available}")
    return parse_json_explain(rows[expected])


def explain_text(connection: duckdb.DuckDBPyConnection, sql: str, *, analyze: bool) -> str:
    rows = _explain_rows(connection, sql, analyze=analyze, json_format=False)
    return "\n\n".join(value for _, value in rows) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _normalized_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, (date, datetime)):
        return {"date": value.isoformat()}
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "nan"}
        if math.isinf(value):
            return {"float": "inf" if value > 0 else "-inf"}
        return {"float": format(value, ".17g")}
    return {"string": str(value)}


def normalized_result(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    normalized = [[_normalized_value(value) for value in row] for row in rows]
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))


def result_checksum(rows: Sequence[Sequence[Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def execute_result(connection: duckdb.DuckDBPyConnection, sql: str) -> tuple[list[list[Any]], str]:
    rows = normalized_result(connection.execute(sql).fetchall())
    return rows, result_checksum(rows)


def _join_rows(
    root: dict[str, Any] | list[Any], run_id: str, query_id: int, scenario: str, mode: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    distinguish_nation_roles = _nation_scan_count(root) > 1

    def visit(node: Mapping[str, Any], path: tuple[int, ...]) -> None:
        children = _children(node)
        if _is_join(node) and len(children) >= 2:
            left, right = children[0], children[1]
            left_actual = _actual_cardinality(left)
            right_actual = _actual_cardinality(right)
            actual = _actual_cardinality(node)
            extra = node.get("extra_info") or {}
            association, ordered = (
                _fingerprint_node(
                    node,
                    ordered=False,
                    distinguish_nation_roles=distinguish_nation_roles,
                    include_join_metadata=False,
                ),
                _fingerprint_node(
                    node,
                    ordered=True,
                    distinguish_nation_roles=distinguish_nation_roles,
                ),
            )
            rows.append(
                {
                    "run_id": run_id,
                    "query_id": query_id,
                    "scenario": scenario,
                    "mode": mode,
                    "node_id": ".".join(str(item) for item in path),
                    "operator_name": _operator_name(node),
                    "join_type": extra.get("Join Type") if isinstance(extra, Mapping) else None,
                    "conditions": _normalize_conditions(extra) if isinstance(extra, Mapping) else "",
                    "left_relations": "|".join(
                        _relation_leaves(left, distinguish_nation_roles)
                    ),
                    "right_relations": "|".join(
                        _relation_leaves(right, distinguish_nation_roles)
                    ),
                    "output_relations": "|".join(
                        _relation_leaves(node, distinguish_nation_roles)
                    ),
                    "left_estimated_cardinality": _estimated_cardinality(left),
                    "right_estimated_cardinality": _estimated_cardinality(right),
                    "estimated_cardinality": _estimated_cardinality(node),
                    "left_actual_cardinality": left_actual,
                    "right_actual_cardinality": right_actual,
                    "actual_cardinality": actual,
                    "output_per_left": safe_ratio(actual, left_actual),
                    "output_per_right": safe_ratio(actual, right_actual),
                    "pair_selectivity": (
                        safe_ratio(actual, left_actual * right_actual)
                        if left_actual is not None and right_actual is not None
                        else None
                    ),
                    "association_node": association,
                    "ordered_node": ordered,
                }
            )
        for index, child in enumerate(children):
            visit(child, (*path, index))

    for index, node in enumerate(_root_nodes(root)):
        visit(node, (index,))
    return rows


def validate_canonical_queries(
    connection: duckdb.DuckDBPyConnection,
    scenarios: Sequence[Scenario],
    official_queries: Mapping[int, str],
    output_dir: Path,
) -> dict[int, dict[str, Any]]:
    validation: dict[int, dict[str, Any]] = {}
    canonical_dir = output_dir / "canonical_validation"
    canonical_dir.mkdir(parents=True, exist_ok=True)
    failures: list[int] = []
    for scenario in scenarios:
        if not scenario.canonical:
            continue
        custom_sql = query_sql(scenario)
        bundled_sql = official_queries.get(scenario.query_id)
        if not bundled_sql:
            raise RuntimeError(f"tpch_queries() did not return Q{scenario.query_id}")
        custom_rows, custom_checksum = execute_result(connection, custom_sql)
        bundled_rows, bundled_checksum = execute_result(connection, bundled_sql)
        matches = custom_rows == bundled_rows
        validation[scenario.query_id] = {
            "matches": matches,
            "custom_rows": len(custom_rows),
            "bundled_rows": len(bundled_rows),
            "custom_checksum": custom_checksum,
            "bundled_checksum": bundled_checksum,
        }
        qdir = canonical_dir / f"q{scenario.query_id:02d}"
        qdir.mkdir(parents=True, exist_ok=True)
        _write_text(qdir / "custom.sql", custom_sql)
        _write_text(qdir / "bundled.sql", bundled_sql)
        _write_json(qdir / "validation.json", validation[scenario.query_id])
        if not matches:
            _write_json(qdir / "custom_result.json", custom_rows)
            _write_json(qdir / "bundled_result.json", bundled_rows)
            failures.append(scenario.query_id)
    if failures:
        joined = ", ".join(f"Q{query_id}" for query_id in failures)
        raise RuntimeError(f"custom canonical templates do not match bundled results: {joined}")
    return validation


def capture_pre_analyze_plans(
    connection: duckdb.DuckDBPyConnection,
    scenarios: Sequence[Scenario],
    output_dir: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not scenario.canonical:
            continue
        sql = query_sql(scenario)
        raw_dir = output_dir / "pre_analyze" / f"q{scenario.query_id:02d}"
        raw_dir.mkdir(parents=True, exist_ok=True)
        with optimizer_mode(connection, "optimized"):
            text_plan = explain_text(connection, sql, analyze=False)
            json_plan = explain_json(connection, sql, analyze=False)
        _write_text(raw_dir / "query.sql", sql)
        _write_text(raw_dir / "explain.txt", text_plan)
        _write_json(raw_dir / "explain.json", json_plan)
        association, ordered = plan_fingerprints(json_plan)
        orientation = plan_orientation_fingerprint(json_plan)
        records.append(
            {
                "query_id": scenario.query_id,
                "association_fingerprint": association,
                "ordered_fingerprint": ordered,
                "orientation_fingerprint": orientation,
                "raw_dir": str(raw_dir.relative_to(output_dir)),
            }
        )
    return records


def profile_scenario(
    connection: duckdb.DuckDBPyConnection,
    scenario: Scenario,
    mode: str,
    repetitions: int,
    output_dir: Path,
    canonical_match: bool | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Capture all raw plans and return scenario/operator/join rows."""

    sql = query_sql(scenario)
    raw_dir = output_dir / "plans" / f"q{scenario.query_id:02d}" / scenario.name / mode
    raw_dir.mkdir(parents=True, exist_ok=True)
    _write_text(raw_dir / "query.sql", sql)

    scenario_rows: list[dict[str, Any]] = []
    operator_rows: list[dict[str, Any]] = []
    join_rows: list[dict[str, Any]] = []

    with optimizer_mode(connection, mode):
        static_text = explain_text(connection, sql, analyze=False)
        static_json = explain_json(connection, sql, analyze=False)
        _write_text(raw_dir / "explain.txt", static_text)
        _write_json(raw_dir / "explain.json", static_json)
        static_association, static_ordered = plan_fingerprints(static_json)
        static_orientation = plan_orientation_fingerprint(static_json)
        static_metadata = plan_metadata_fingerprint(static_json)

        # Execute once before profiling so optimized and forced modes both see
        # materialized buffers.  Results are small and double as an integrity check.
        result_rows, checksum = execute_result(connection, sql)
        _write_json(raw_dir / "result.json", result_rows)

        analyzed_roots: list[dict[str, Any] | list[Any]] = []
        for repetition in range(1, repetitions + 1):
            started = time.perf_counter()
            analyzed_json = explain_json(connection, sql, analyze=True)
            wall_seconds = time.perf_counter() - started
            analyzed_roots.append(analyzed_json)
            filename = "explain_analyze.json" if repetitions == 1 else f"explain_analyze_rep{repetition}.json"
            _write_json(raw_dir / filename, analyzed_json)

            run_id = f"q{scenario.query_id:02d}.{scenario.name}.{mode}.r{repetition}"
            flattened = flatten_plan(analyzed_json, scenario.query_id, scenario.name, mode)
            for row in flattened:
                row["run_id"] = run_id
                row["repetition"] = repetition
            joins = _join_rows(analyzed_json, run_id, scenario.query_id, scenario.name, mode)
            operator_rows.extend(flattened)
            join_rows.extend(joins)

            association, ordered = plan_fingerprints(analyzed_json)
            orientation = plan_orientation_fingerprint(analyzed_json)
            join_cards = [row["actual_cardinality"] for row in joins if row["actual_cardinality"] is not None]
            root_latency = analyzed_json.get("latency") if isinstance(analyzed_json, Mapping) else None
            scenario_rows.append(
                {
                    "run_id": run_id,
                    "query_id": scenario.query_id,
                    "scenario": scenario.name,
                    "scenario_family": scenario.family,
                    "scenario_order": scenario.order,
                    "canonical": scenario.canonical,
                    "negative_control": scenario.negative_control,
                    "parameters_json": json.dumps(scenario.parameters, sort_keys=True),
                    "optimizer_mode": mode,
                    "repetition": repetition,
                    "result_rows": len(result_rows),
                    "result_checksum": checksum,
                    "canonical_template_match": canonical_match,
                    "association_fingerprint": association,
                    "ordered_fingerprint": ordered,
                    "orientation_fingerprint": orientation,
                    "static_analyzed_fingerprint_match": (
                        association == static_association and ordered == static_ordered
                    ),
                    "max_join_actual_rows": max(join_cards) if join_cards else None,
                    "profile_latency_seconds": root_latency,
                    "profile_wall_seconds": wall_seconds,
                    "raw_dir": str(raw_dir.relative_to(output_dir)),
                }
            )

        # Required human-readable EXPLAIN ANALYZE.  It is a separate executed
        # run; structured runs above remain the source for recorded metrics.
        analyzed_text = explain_text(connection, sql, analyze=True)
        _write_text(raw_dir / "explain_analyze.txt", analyzed_text)

    summary = {
        "scenario": scenario,
        "static_json": static_json,
        "static_fingerprints": (static_association, static_ordered),
        "static_orientation_fingerprint": static_orientation,
        "static_metadata_fingerprint": static_metadata,
        "raw_dir": str(raw_dir.relative_to(output_dir)),
        "analyzed_roots": analyzed_roots,
    }
    return scenario_rows, operator_rows, join_rows, summary


def _transition_value(scenario: Scenario) -> int:
    if scenario.query_id == 3:
        return date.fromisoformat(str(scenario.parameters["cutoff"])).toordinal()
    if scenario.query_id == 5:
        return date.fromisoformat(str(scenario.parameters["end"])).toordinal()
    if scenario.query_id == 9:
        return int(scenario.parameters["threshold"])
    raise ValueError(f"Q{scenario.query_id} has no refinable axis")


def _transition_display(query_id: int, value: int) -> str:
    if query_id in (3, 5):
        return date.fromordinal(value).isoformat()
    return str(value)


def _transition_scenario(template: Scenario, value: int) -> Scenario:
    parameters = dict(template.parameters)
    display = _transition_display(template.query_id, value)
    if template.query_id == 3:
        parameters["cutoff"] = display
    elif template.query_id == 5:
        parameters["end"] = display
    elif template.query_id == 9:
        parameters["threshold"] = value
        parameters["part_predicate"] = f"p.p_partkey <= {value}"
    else:
        raise ValueError(f"Q{template.query_id} has no refinable axis")
    safe_name = display.replace("-", "_")
    return Scenario(
        template.query_id,
        f"transition_probe_{safe_name}",
        template.family,
        value,
        f"transition probe at {display}",
        parameters,
    )


def _transition_selectivity(
    connection: duckdb.DuckDBPyConnection, scenario: Scenario
) -> dict[str, float | None]:
    p = scenario.parameters
    if scenario.query_id == 3:
        cutoff = _date_literal(str(p["cutoff"]))
        order_total, order_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE o_orderdate < {cutoff}) FROM orders"
        ).fetchone()
        line_total, line_selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE l_shipdate > {cutoff}) FROM lineitem"
        ).fetchone()
        return {
            "orders": safe_ratio(order_selected, order_total),
            "lineitem": safe_ratio(line_selected, line_total),
        }
    if scenario.query_id == 5:
        start = _date_literal(str(p["start"]))
        end = _date_literal(str(p["end"]))
        total, selected = connection.execute(
            f"""
SELECT count(*), count(*) FILTER (
    WHERE o_orderdate >= {start} AND o_orderdate < {end}
) FROM orders
"""
        ).fetchone()
        return {"orders": safe_ratio(selected, total)}
    if scenario.query_id == 9:
        threshold = int(p["threshold"])
        total, selected = connection.execute(
            f"SELECT count(*), count(*) FILTER (WHERE p_partkey <= {threshold}) FROM part"
        ).fetchone()
        return {"part": safe_ratio(selected, total)}
    return {}


def refine_transitions(
    connection: duckdb.DuckDBPyConnection,
    scenarios: Sequence[Scenario],
    profile_summaries: Mapping[tuple[int, str, str], dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Find one discrete boundary between coarse endpoints with different plans."""

    comparable: list[list[Scenario]] = []
    for query_id, family in (
        (3, "shared_date_cutoff"),
        (5, "date_window_end"),
        (9, "partkey_fraction"),
    ):
        group = sorted(
            [s for s in scenarios if s.query_id == query_id and s.family == family],
            key=_transition_value,
        )
        if len(group) > 1:
            comparable.append(group)

    # association, ordered, orientation-only, orientation-free metadata, raw path
    cache: dict[tuple[int, int], tuple[str, str, str, str, str]] = {}

    def fingerprint(scenario: Scenario, value: int) -> tuple[str, str, str, str, str]:
        key = (scenario.query_id, value)
        if key in cache:
            return cache[key]
        probe = _transition_scenario(scenario, value)
        sql = query_sql(probe)
        display = _transition_display(probe.query_id, value).replace("-", "_")
        raw_dir = output_dir / "transitions" / f"q{probe.query_id:02d}" / probe.family / display
        raw_dir.mkdir(parents=True, exist_ok=True)
        with optimizer_mode(connection, "optimized"):
            json_plan = explain_json(connection, sql, analyze=False)
            text_plan = explain_text(connection, sql, analyze=False)
        _write_text(raw_dir / "query.sql", sql)
        _write_json(raw_dir / "explain.json", json_plan)
        _write_text(raw_dir / "explain.txt", text_plan)
        association, ordered = plan_fingerprints(json_plan)
        orientation = plan_orientation_fingerprint(json_plan)
        metadata_fingerprint = plan_metadata_fingerprint(json_plan)
        cache[key] = (
            association,
            ordered,
            orientation,
            metadata_fingerprint,
            str(raw_dir.relative_to(output_dir)),
        )
        return cache[key]

    transitions: list[dict[str, Any]] = []
    for group in comparable:
        for low_scenario, high_scenario in zip(group, group[1:]):
            low_value = _transition_value(low_scenario)
            high_value = _transition_value(high_scenario)
            low_summary = profile_summaries[(low_scenario.query_id, low_scenario.name, "optimized")]
            high_summary = profile_summaries[(high_scenario.query_id, high_scenario.name, "optimized")]
            low_plan = (
                *low_summary["static_fingerprints"],
                low_summary["static_orientation_fingerprint"],
                low_summary["static_metadata_fingerprint"],
            )
            high_plan = (
                *high_summary["static_fingerprints"],
                high_summary["static_orientation_fingerprint"],
                high_summary["static_metadata_fingerprint"],
            )
            if low_plan[:2] == high_plan[:2]:
                continue

            # Seed the probe cache with endpoint plans already captured by the
            # main experiment.  Only midpoint probes create transition files.
            cache[(low_scenario.query_id, low_value)] = (
                *low_plan,
                low_summary["raw_dir"],
            )
            cache[(high_scenario.query_id, high_value)] = (
                *high_plan,
                high_summary["raw_dir"],
            )
            probes = 0
            while high_value - low_value > 1:
                midpoint = (low_value + high_value) // 2
                mid_assoc, mid_ordered, mid_orientation, mid_metadata, _ = fingerprint(
                    low_scenario, midpoint
                )
                probes += 1
                if (mid_assoc, mid_ordered) == low_plan[:2]:
                    low_value = midpoint
                    low_plan = (mid_assoc, mid_ordered, mid_orientation, mid_metadata)
                else:
                    high_value = midpoint
                    high_plan = (mid_assoc, mid_ordered, mid_orientation, mid_metadata)

            low_probe = _transition_scenario(low_scenario, low_value)
            high_probe = _transition_scenario(high_scenario, high_value)
            (
                low_assoc, low_ordered, low_orientation, low_metadata, low_raw_dir
            ) = fingerprint(
                low_scenario, low_value
            )
            (
                high_assoc, high_ordered, high_orientation, high_metadata, high_raw_dir
            ) = fingerprint(
                high_scenario, high_value
            )
            classification = classify_plan_change(
                low_assoc,
                high_assoc,
                low_orientation,
                high_orientation,
                low_metadata,
                high_metadata,
            )
            transitions.append(
                {
                    "query_id": low_scenario.query_id,
                    "scenario_family": low_scenario.family,
                    "parameter_name": (
                        "cutoff" if low_scenario.query_id == 3 else
                        "end" if low_scenario.query_id == 5 else "partkey_threshold"
                    ),
                    "parameter_type": "date" if low_scenario.query_id in (3, 5) else "integer",
                    "low_value": _transition_display(low_scenario.query_id, low_value),
                    "high_value": _transition_display(high_scenario.query_id, high_value),
                    "low_realized_selectivity_json": json.dumps(_transition_selectivity(connection, low_probe), sort_keys=True),
                    "high_realized_selectivity_json": json.dumps(_transition_selectivity(connection, high_probe), sort_keys=True),
                    "low_association_fingerprint": low_assoc,
                    "high_association_fingerprint": high_assoc,
                    "low_ordered_fingerprint": low_ordered,
                    "high_ordered_fingerprint": high_ordered,
                    "low_orientation_fingerprint": low_orientation,
                    "high_orientation_fingerprint": high_orientation,
                    "low_metadata_fingerprint": low_metadata,
                    "high_metadata_fingerprint": high_metadata,
                    **classification,
                    "resolution": "one day" if low_scenario.query_id in (3, 5) else "one part key",
                    "probe_count": probes,
                    "low_raw_dir": low_raw_dir,
                    "high_raw_dir": high_raw_dir,
                    "search_scope": "endpoint_triggered_non_exhaustive",
                }
            )
    return transitions


def _audit_value(
    audit_rows: Sequence[Mapping[str, Any]], query_id: int, scenario: str, audit_id: str
) -> float:
    scenario_id = f"q{query_id:02d}.{scenario}"
    matches = [
        row for row in audit_rows
        if row["scenario_id"] == scenario_id and row["audit_id"] == audit_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one audit {scenario_id}/{audit_id}, found {len(matches)}")
    return float(matches[0]["value"])


def validate_audit_monotonicity(audit_rows: Sequence[Mapping[str, Any]]) -> None:
    checks: list[tuple[str, Sequence[float], bool]] = []
    available = {(int(row["query_id"]), str(row["scenario_id"]).split(".", 1)[1]) for row in audit_rows}
    if all((1, name) in available for name in ("shipdate_p05", "shipdate_p50", "shipdate_p95", "canonical")):
        checks.append((
            "Q1 shipdate counts",
            [_audit_value(audit_rows, 1, name, "lineitem_shipdate") for name in ("shipdate_p05", "shipdate_p50", "shipdate_p95", "canonical")],
            True,
        ))
    if all((3, name) in available for name in ("date_early", "canonical", "date_late")):
        checks.extend((
            ("Q3 order selectivity", [_audit_value(audit_rows, 3, name, "orders_date") for name in ("date_early", "canonical", "date_late")], True),
            ("Q3 ship selectivity", [_audit_value(audit_rows, 3, name, "lineitem_shipdate") for name in ("date_early", "canonical", "date_late")], False),
        ))
    if all((5, name) in available for name in ("window_one_month", "canonical", "window_wide")):
        checks.extend((
            ("Q5 date selectivity", [_audit_value(audit_rows, 5, name, "orders_date") for name in ("window_one_month", "canonical", "window_wide")], True),
            ("Q5 full rows", [_audit_value(audit_rows, 5, name, "full_preaggregate") for name in ("window_one_month", "canonical", "window_wide")], True),
        ))
    if all((8, name) in available for name in ("canonical", "type_family", "type_unrestricted")):
        checks.append((
            "Q8 part selectivity",
            [_audit_value(audit_rows, 8, name, "part_type") for name in ("canonical", "type_family", "type_unrestricted")],
            True,
        ))
    if all((9, name) in available for name in ("partkey_p00025", "partkey_p05", "partkey_p80")):
        checks.append((
            "Q9 numeric selectivity",
            [_audit_value(audit_rows, 9, name, "part_filter") for name in ("partkey_p00025", "partkey_p05", "partkey_p80")],
            True,
        ))

    failures: list[str] = []
    for label, values, increasing in checks:
        pairs = zip(values, values[1:])
        okay = all(left <= right for left, right in pairs) if increasing else all(left >= right for left, right in pairs)
        if not okay:
            failures.append(f"{label}: {values}")
    if failures:
        raise RuntimeError("non-monotonic selectivity audits:\n" + "\n".join(failures))


def validate_plan_controls(
    scenario_rows: Sequence[Mapping[str, Any]],
    operator_rows: Sequence[Mapping[str, Any]],
    join_rows: Sequence[Mapping[str, Any]],
    audit_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    unstable = [row["run_id"] for row in scenario_rows if not row["static_analyzed_fingerprint_match"]]
    result_checksums: dict[tuple[int, str], set[str]] = {}
    for row in scenario_rows:
        if row.get("result_checksum") is None:
            continue
        result_checksums.setdefault(
            (int(row["query_id"]), str(row["scenario"])), set()
        ).add(str(row["result_checksum"]))
    result_mismatches = [
        f"Q{query_id}/{scenario}"
        for (query_id, scenario), checksums in result_checksums.items()
        if len(checksums) != 1
    ]
    cross_products = [
        row["run_id"] for row in operator_rows
        if row["mode"] == "forced" and str(row["operator_name"]).upper() == "CROSS_PRODUCT"
    ]
    if unstable:
        raise RuntimeError(f"static/analyzed join fingerprints differ: {unstable}")
    if result_mismatches:
        raise RuntimeError(
            "optimized/forced or repeated runs returned different results: "
            + ", ".join(result_mismatches)
        )
    if cross_products:
        raise RuntimeError(f"forced connected plans unexpectedly contain cross products: {cross_products}")

    expected_forced_orders = {
        3: ("customer", "orders", "lineitem"),
        5: ("region", "nation", "customer", "orders", "lineitem", "supplier"),
        8: (
            "part", "lineitem", "orders", "customer", "nation_region",
            "region", "supplier", "nation_name",
        ),
        9: ("nation", "supplier", "lineitem", "part", "orders", "partsupp"),
    }
    forced_runs = {
        str(row["run_id"]): int(row["query_id"])
        for row in scenario_rows
        if row["optimizer_mode"] == "forced" and int(row["repetition"]) == 1
    }
    wrong_orders: list[str] = []
    for run_id, query_id in forced_runs.items():
        expected = expected_forced_orders.get(query_id)
        if expected is None:
            continue
        observed = sorted(
            (row for row in join_rows if row["run_id"] == run_id),
            key=lambda row: len(str(row["output_relations"]).split("|")),
        )
        expected_steps = []
        prefix = frozenset((expected[0],))
        for relation in expected[1:]:
            expected_steps.append((prefix, frozenset((relation,)), prefix | {relation}))
            prefix = prefix | {relation}
        observed_steps = [
            (
                frozenset(str(row["left_relations"]).split("|")),
                frozenset(str(row["right_relations"]).split("|")),
                frozenset(str(row["output_relations"]).split("|")),
            )
            for row in observed
        ]
        if observed_steps != expected_steps:
            wrong_orders.append(run_id)
    if wrong_orders:
        raise RuntimeError(f"forced plans do not match their declared left-deep order: {wrong_orders}")

    q8_mode = (
        "optimized"
        if any(
            row["query_id"] == 8 and row["optimizer_mode"] == "optimized"
            for row in scenario_rows
        )
        else "forced"
    )
    q8_rows = [
        row for row in scenario_rows
        if row["query_id"] == 8
        and row["optimizer_mode"] == q8_mode
        and row["repetition"] == 1
    ]
    by_name = {str(row["scenario"]): row for row in q8_rows}
    q8_check: dict[str, Any] = {"status": "not_run"}
    if "canonical" in by_name and "case_nation_control" in by_name:
        canonical = by_name["canonical"]
        control = by_name["case_nation_control"]
        q8_audits: dict[str, dict[str, tuple[Any, Any, Any]]] = {}
        for scenario_name in ("canonical", "case_nation_control"):
            scenario_id = f"q08.{scenario_name}"
            q8_audits[scenario_name] = {
                str(row["audit_id"]): (
                    row.get("numerator"), row.get("denominator"), row.get("value")
                )
                for row in audit_rows
                if row["scenario_id"] == scenario_id
            }
        canonical_join_cards = sorted(
            (
                str(row["node_id"]),
                row.get("left_actual_cardinality"),
                row.get("right_actual_cardinality"),
                row.get("actual_cardinality"),
            )
            for row in join_rows
            if row["run_id"] == canonical["run_id"]
        )
        control_join_cards = sorted(
            (
                str(row["node_id"]),
                row.get("left_actual_cardinality"),
                row.get("right_actual_cardinality"),
                row.get("actual_cardinality"),
            )
            for row in join_rows
            if row["run_id"] == control["run_id"]
        )
        invariant = (
            canonical["association_fingerprint"] == control["association_fingerprint"]
            and canonical["ordered_fingerprint"] == control["ordered_fingerprint"]
            and q8_audits["canonical"] == q8_audits["case_nation_control"]
            and canonical_join_cards == control_join_cards
        )
        if not invariant:
            raise RuntimeError(
                "Q8 CASE-nation negative control changed its plan or a filtered/intermediate cardinality"
            )
        parameters = json.loads(str(control["parameters_json"]))
        contributing_control = bool(parameters.get("control_has_contributing_rows"))
        result_changed = canonical["result_checksum"] != control["result_checksum"]
        if contributing_control and not result_changed:
            raise RuntimeError(
                "Q8 CASE-nation control selected a contributing nation but did not change the result"
            )
        q8_check = {
            "status": "passed" if result_changed else "inconclusive",
            "plan_and_cardinality_invariant": True,
            "result_changed": result_changed,
            "control_has_contributing_rows": contributing_control,
            "note": (
                "inconclusive_no_contributing_alternate_nation" if not result_changed
                else "CASE numerator changed as expected"
            ),
        }
    return {
        "result_equivalence": {
            "status": "passed",
            "scenario_count": len(result_checksums),
            "optimizer_modes": sorted(
                {str(row["optimizer_mode"]) for row in scenario_rows}
            ),
        },
        "q8_negative_control": q8_check,
    }


def _csv_fieldnames(rows: Sequence[Mapping[str, Any]], preferred: Sequence[str]) -> list[str]:
    fields = list(preferred)
    seen = set(fields)
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    return fields


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], preferred: Sequence[str]) -> None:
    fieldnames = _csv_fieldnames(rows, preferred)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _fingerprint_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _format_number(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        if math.isinf(value):
            return "∞"
        if abs(value) < 0.01 and value != 0:
            return f"{value:.3e}"
        return f"{value:.4f}"
    return str(value)


def generate_report(
    output_dir: Path,
    metadata: Mapping[str, Any],
    scenarios: Sequence[Scenario],
    scenario_rows: Sequence[Mapping[str, Any]],
    operator_rows: Sequence[Mapping[str, Any]],
    audit_rows: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    canonical_validation: Mapping[int, Mapping[str, Any]],
    pre_analyze: Sequence[Mapping[str, Any]],
    validations: Mapping[str, Any],
) -> None:
    first_runs = [row for row in scenario_rows if int(row["repetition"]) == 1]
    first_run_by_key = {
        (int(row["query_id"]), str(row["scenario"]), str(row["optimizer_mode"])): row
        for row in first_runs
    }
    audit_by_key = {
        (
            int(row.get("query_id", str(row["scenario_id"])[1:3])),
            str(row["scenario_id"]).split(".", 1)[1],
            str(row["audit_id"]),
        ): row
        for row in audit_rows
    }
    qerrors: dict[str, list[float]] = {}
    for row in operator_rows:
        value = row.get("q_error")
        if value is not None:
            qerrors.setdefault(str(row["run_id"]), []).append(float(value))

    def audit_ratio(query_id: int, scenario_name: str, audit_id: str) -> float | None:
        row = audit_by_key.get((query_id, scenario_name, audit_id))
        return float(row["value"]) if row is not None and row.get("value") is not None else None

    def selectivity_label(query_id: int, scenario_name: str) -> str:
        ids = {
            3: ("orders_date", "lineitem_shipdate"),
            5: ("orders_date",),
            8: ("part_type",),
            9: ("part_filter",),
        }.get(query_id, ())
        values = [audit_ratio(query_id, scenario_name, audit_id) for audit_id in ids]
        if query_id == 3 and all(value is not None for value in values):
            return f"orders {100 * values[0]:.2f}%, ship {100 * values[1]:.2f}%"
        if values and values[0] is not None:
            return f"{100 * values[0]:.2f}%"
        return "selectivity unavailable"

    lines = [
        "# DuckDB TPC-H Selectivity and Join-Order Report",
        "",
        "## Reproducibility",
        "",
        f"- Generated: `{metadata['finished_at_utc']}`",
        f"- DuckDB: `{metadata['duckdb_python_version']}`",
        f"- Scale factor: `{metadata['scale_factor']}`",
        f"- Threads: `{metadata['threads']}`; repetitions: `{metadata['repetitions']}`",
        f"- Database: `{metadata['database']}`",
        f"- Git commit: `{metadata.get('git_commit') or 'unknown'}`; dirty: `{metadata.get('git_dirty')}`",
        "- Operator timings are diagnostic. Cardinalities, selectivities, and plan fingerprints are the primary evidence.",
        "",
        "## Canonical validation",
        "",
        "| Query | ANSI template equals bundled query | Rows | Checksum |",
        "|---:|:---:|---:|---|",
    ]
    for query_id in sorted(canonical_validation):
        item = canonical_validation[query_id]
        lines.append(
            f"| Q{query_id} | {'yes' if item['matches'] else 'NO'} | {item['custom_rows']} | `{item['custom_checksum'][:12]}` |"
        )
    equivalence = validations.get("result_equivalence", {})
    if equivalence.get("status") == "passed":
        result_modes = equivalence.get("optimizer_modes", [])
        if len(result_modes) > 1:
            equivalence_text = (
                "Result executions agreed across all selected optimizer modes for "
                f"{equivalence.get('scenario_count', 0)} logical scenarios."
            )
        else:
            equivalence_text = (
                f"Result checksums were recorded in `{result_modes[0] if result_modes else 'unknown'}` "
                f"mode for {equivalence.get('scenario_count', 0)} logical scenarios."
            )
        lines.extend(
            (
                "",
                equivalence_text,
            )
        )

    lines.extend(
        (
            "",
            "## Realized authored selectivities",
            "",
            "These values come from independent `COUNT` audits, not physical scan output. Runtime filters may reduce scan output further.",
            "",
            "| Query/scenario | Scope | Audit | Numerator | Denominator | Value | Unit |",
            "|---|---|---|---:|---:|---:|---|",
        )
    )
    for row in audit_rows:
        if row["audit_kind"] not in (
            "base_predicate", "base_path", "derived", "correlation", "integrity"
        ):
            continue
        if row["audit_kind"] == "integrity" and not str(row["scenario_id"]).endswith(
            ".canonical"
        ):
            continue
        lines.append(
            "| {scenario} | {scope} | {audit} | {num} | {den} | {value} | {unit} |".format(
                scenario=str(row["scenario_id"]),
                scope=str(row["scope"]),
                audit=str(row["audit_id"]),
                num=_format_number(row["numerator"]),
                den=_format_number(row["denominator"]),
                value=_format_number(row["value"]),
                unit=str(row.get("unit", "")),
            )
        )

    lines.extend(
        (
            "",
            "## Join-order findings",
            "",
            "The association tree shows which relation sets join first and ignores probe/build orientation. "
            "Percentages are independently audited realized selectivities.",
            "",
        )
    )
    for query_id in (3, 5, 8, 9):
        query_scenarios = [
            scenario for scenario in scenarios
            if scenario.query_id == query_id and not scenario.negative_control
        ]
        families: dict[str, list[Scenario]] = {}
        for scenario in query_scenarios:
            families.setdefault(scenario.family, []).append(scenario)
        for family, family_scenarios in families.items():
            entries = []
            for scenario in sorted(family_scenarios, key=lambda item: item.order):
                run = first_run_by_key.get((query_id, scenario.name, "optimized"))
                if run is not None:
                    entries.append((scenario, run))
            if not entries:
                continue
            associations = {str(run["association_fingerprint"]) for _, run in entries}
            orientations = {str(run["orientation_fingerprint"]) for _, run in entries}
            if len(entries) == 1:
                scenario, run = entries[0]
                lines.append(
                    f"- **Q{query_id} `{family}`:** `{scenario.name}` realized "
                    f"{selectivity_label(query_id, scenario.name)}; tree "
                    f"`{run['association_fingerprint']}`."
                )
                continue
            labels = " → ".join(
                f"`{scenario.name}` ({selectivity_label(query_id, scenario.name)})"
                for scenario, _ in entries
            )
            lines.append(
                f"- **Q{query_id} `{family}`:** association "
                f"{'changed' if len(associations) > 1 else 'stayed fixed'}; "
                + (
                    "orientation is not globally comparable across different associations"
                    if len(associations) > 1
                    else f"orientation {'changed' if len(orientations) > 1 else 'stayed fixed'}"
                )
                + f". Scenarios: {labels}."
            )
            if len(associations) == 1:
                lines.append(f"  Tree: `{next(iter(associations))}`")
            else:
                for scenario, run in entries:
                    lines.append(
                        f"  - `{scenario.name}`: `{run['association_fingerprint']}`"
                    )
            if len(associations) == 1 and len(orientations) > 1:
                lines.append("  Probe/build-orientation trees (for hash joins):")
                for scenario, run in entries:
                    lines.append(
                        f"  - `{scenario.name}`: `{run['orientation_fingerprint']}`"
                    )

    q9_like = first_run_by_key.get((9, "canonical", "optimized"))
    q9_numeric = first_run_by_key.get((9, "partkey_p05", "optimized"))
    if q9_like is not None and q9_numeric is not None:
        q9_association_same = (
            q9_like["association_fingerprint"] == q9_numeric["association_fingerprint"]
        )
        q9_orientation_same = (
            q9_like["orientation_fingerprint"] == q9_numeric["orientation_fingerprint"]
        )
        q9_orientation_finding = (
            f"{'the same' if q9_orientation_same else 'different'} probe/build orientations"
            if q9_association_same
            else "non-comparable orientations because the associations differ"
        )
        lines.extend(
            (
                "",
                "Q9's `%green%` and numeric 5% controls have "
                f"{'the same' if q9_association_same else 'different'} association trees and "
                f"{q9_orientation_finding}. "
                "This is evidence about predicate form and join-key range propagation, "
                "not a clean LIKE-estimator-only comparison: `p_partkey` is itself a join key, while `p_name` is not.",
            )
        )
        if q9_association_same and not q9_orientation_same:
            lines.extend(
                (
                    f"- `%green%` orientation: `{q9_like['orientation_fingerprint']}`",
                    f"- Numeric 5% orientation: `{q9_numeric['orientation_fingerprint']}`",
                )
            )
        q9_retention = audit_ratio(9, "canonical", "partsupp_retention")
        q9_fanout = audit_ratio(9, "canonical", "partsupp_match_fanout")
        q9_uniqueness = audit_ratio(9, "canonical", "partsupp_key_uniqueness")
        if all(value is not None for value in (q9_retention, q9_fanout, q9_uniqueness)):
            lines.extend((
                "",
                "Standard DBGEN Q9 composite-join audit: matched-lineitem retention "
                f"{q9_retention:.4f}, match fanout {q9_fanout:.4f}, and partsupp "
                f"composite-key uniqueness {q9_uniqueness:.4f}.",
            ))

    paired_plan_rows = []
    for scenario in scenarios:
        if scenario.query_id == 1:
            continue
        optimized = first_run_by_key.get((scenario.query_id, scenario.name, "optimized"))
        forced = first_run_by_key.get((scenario.query_id, scenario.name, "forced"))
        if optimized is not None and forced is not None:
            paired_plan_rows.append((scenario, optimized, forced))
    if paired_plan_rows:
        lines.extend(
            (
                "",
                "### Optimized versus forced SQL order",
                "",
                "| Query/scenario | Same association? | Same orientation? | Optimized peak join rows | Forced peak join rows | Forced/optimized peak |",
                "|---|:---:|:---:|---:|---:|---:|",
            )
        )
        for scenario, optimized, forced in paired_plan_rows:
            peak_ratio = safe_ratio(
                forced.get("max_join_actual_rows"), optimized.get("max_join_actual_rows")
            )
            same_association = (
                optimized["association_fingerprint"] == forced["association_fingerprint"]
            )
            same_orientation = (
                optimized["orientation_fingerprint"] == forced["orientation_fingerprint"]
            )
            orientation_label = (
                "n/a" if not same_association else "yes" if same_orientation else "no"
            )
            lines.append(
                f"| Q{scenario.query_id}/{scenario.name} | "
                f"{'yes' if same_association else 'no'} | "
                f"{orientation_label} | "
                f"{_format_number(optimized.get('max_join_actual_rows'))} | "
                f"{_format_number(forced.get('max_join_actual_rows'))} | "
                f"{_format_number(peak_ratio)} |"
            )

    lines.extend(
        (
            "",
            "## Optimized and forced plans",
            "",
            "Association IDs ignore child orientation; ordered IDs preserve DuckDB's left/right (probe/build for hash joins) orientation.",
            "",
            "| Query/scenario | Mode | Association | Ordered | Max join rows | Median/max finite q-error | ∞ q-error nodes | Profile seconds | Raw plans |",
            "|---|---|---|---|---:|---:|---:|---:|---|",
        )
    )
    for row in first_runs:
        run_qerrors = qerrors.get(str(row["run_id"]), [])
        finite_qerrors = [value for value in run_qerrors if math.isfinite(value)]
        finite_summary = (
            f"{_format_number(statistics.median(finite_qerrors))} / {_format_number(max(finite_qerrors))}"
            if finite_qerrors else "–"
        )
        infinite_count = sum(math.isinf(value) for value in run_qerrors)
        lines.append(
            "| Q{query}/{scenario} | {mode} | `{assoc}` | `{ordered}` | {max_rows} | {qerr} | {infinite} | {seconds} | [{raw}]({raw}) |".format(
                query=row["query_id"],
                scenario=row["scenario"],
                mode=row["optimizer_mode"],
                assoc=_fingerprint_id(str(row["association_fingerprint"])),
                ordered=_fingerprint_id(str(row["ordered_fingerprint"])),
                max_rows=_format_number(row["max_join_actual_rows"]),
                qerr=finite_summary,
                infinite=infinite_count,
                seconds=_format_number(row["profile_latency_seconds"]),
                raw=row["raw_dir"],
            )
        )

    swept_audits = {
        1: ("lineitem_shipdate",),
        3: ("orders_date", "lineitem_shipdate"),
        5: ("orders_date",),
        8: ("part_type",),
        9: ("part_filter",),
    }
    estimate_evidence: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario.negative_control:
            continue
        run = first_run_by_key.get((scenario.query_id, scenario.name, "optimized"))
        if run is None:
            continue
        base_audits = [
            row for (query_id, scenario_name, _), row in audit_by_key.items()
            if query_id == scenario.query_id
            and scenario_name == scenario.name
            and row["audit_kind"] == "base_predicate"
        ]
        for audit in base_audits:
            audit_id = str(audit["audit_id"])
            if (
                audit_id not in swept_audits.get(scenario.query_id, ())
                and not scenario.canonical
            ):
                continue
            scans = [
                row for row in operator_rows
                if row.get("run_id") == run["run_id"]
                and "SCAN" in str(row.get("operator_name", "")).upper()
                and str(row.get("relation_set", "")) == str(audit["scope"])
            ]
            if len(scans) != 1:
                raise RuntimeError(
                    f"expected exactly one optimized scan for Q{scenario.query_id} "
                    f"{scenario.name}/{audit_id}, found {len(scans)}"
                )
            estimate = scans[0].get("estimated_cardinality")
            errors = cardinality_errors(estimate, audit["numerator"])
            estimate_evidence.append(
                {
                    "query_id": scenario.query_id,
                    "scenario": scenario.name,
                    "audit_id": audit_id,
                    "actual": audit["numerator"],
                    "total": audit["denominator"],
                    "estimated": estimate,
                    "q_error": errors["q_error"],
                }
            )

    if estimate_evidence:
        lines.extend(
            (
                "",
                "### Filter-estimation evidence (optimized mode)",
                "",
                "The actual column is the independent authored-predicate count. The estimate is read from "
                "the corresponding scan; analyzed scan output is deliberately not used as ground truth because runtime filters can reduce it.",
                "",
                "| Query/scenario | Predicate audit | Audited rows | Realized | Scan estimate | Estimated | q-error |",
                "|---|---|---:|---:|---:|---:|---:|",
            )
        )
        for item in estimate_evidence:
            actual_ratio = safe_ratio(item["actual"], item["total"])
            estimated_ratio = safe_ratio(item["estimated"], item["total"])
            lines.append(
                f"| Q{item['query_id']}/{item['scenario']} | `{item['audit_id']}` | "
                f"{_format_number(item['actual'])} | "
                f"{_format_number(100 * actual_ratio) if actual_ratio is not None else '–'}% | "
                f"{_format_number(item['estimated'])} | "
                f"{_format_number(100 * estimated_ratio) if estimated_ratio is not None else '–'}% | "
                f"{_format_number(item['q_error'])} |"
            )

        evidence_groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for item in estimate_evidence:
            evidence_groups.setdefault((item["query_id"], item["audit_id"]), []).append(item)
        for (query_id, audit_id), items in evidence_groups.items():
            estimates = {item["estimated"] for item in items}
            actuals = {item["actual"] for item in items}
            if len(items) > 1 and len(estimates) == 1 and len(actuals) > 1:
                lines.append(
                    f"- Q{query_id} `{audit_id}`: DuckDB kept the scan estimate at "
                    f"{_format_number(next(iter(estimates)))} rows while audited rows ranged from "
                    f"{_format_number(min(actuals))} to {_format_number(max(actuals))}."
                )

        for query_id, audit_id in ((3, "orders_date"), (5, "orders_date")):
            items = evidence_groups.get((query_id, audit_id), [])
            runs = [
                run for (qid, _, mode), run in first_run_by_key.items()
                if qid == query_id and mode == "optimized"
            ]
            if (
                len(items) > 1
                and len({item["estimated"] for item in items}) == 1
                and len({str(run["association_fingerprint"]) for run in runs}) == 1
            ):
                lines.append(
                    f"- Interpretation for Q{query_id}: the realized range selectivity moved, but "
                    "the scan estimate and association tree stayed fixed; the coarse sweep therefore "
                    "did not give the join enumerator a changing estimate for that predicate."
                )

        q8_runs = [
            first_run_by_key.get((8, name, "optimized"))
            for name in ("canonical", "type_family", "type_unrestricted")
        ]
        if all(run is not None for run in q8_runs) and len(
            {str(run["association_fingerprint"]) for run in q8_runs if run is not None}
        ) > 1:
            lines.append(
                "- Q8 is the positive contrast: broadening the part predicate from exact type through "
                "the ECONOMY family to unrestricted input changed the optimized association tree."
            )

    optimized_run_ids = {
        str(row["run_id"]): int(row["query_id"])
        for row in first_runs if row["optimizer_mode"] == "optimized"
    }
    worst_by_query: dict[int, Mapping[str, Any]] = {}
    infinite_by_query: dict[int, int] = {}
    for row in operator_rows:
        run_id = str(row.get("run_id", ""))
        query_id = optimized_run_ids.get(run_id)
        q_error = row.get("q_error")
        if query_id is None or q_error is None:
            continue
        if math.isinf(float(q_error)):
            infinite_by_query[query_id] = infinite_by_query.get(query_id, 0) + 1
            continue
        previous = worst_by_query.get(query_id)
        if previous is None or float(q_error) > float(previous["q_error"]):
            worst_by_query[query_id] = row
    if worst_by_query:
        lines.extend(("", "Worst finite-q-error optimized operator per query:", ""))
        for query_id in sorted(worst_by_query):
            row = worst_by_query[query_id]
            lines.append(
                f"- Q{query_id}: `{row['operator_name']}` on `{row.get('relation_set') or 'derived'}` "
                f"in `{row['scenario']}` estimated {_format_number(row.get('estimated_cardinality'))}, "
                f"actual {_format_number(row.get('actual_cardinality'))}, q-error {_format_number(row['q_error'])}."
            )
        if infinite_by_query:
            lines.append(
                "- Operators with conventional infinite q-error (zero estimated or actual cardinality): "
                + ", ".join(
                    f"Q{query_id}={count}"
                    for query_id, count in sorted(infinite_by_query.items())
                )
                + ". See smoothed q-error in `operators.csv` for a finite zero-aware ranking."
            )
    lines.extend(
        (
            "",
            "Forced-mode estimates are not directly comparable: disabling join-order and build/probe-side "
            "optimization can produce zero or otherwise degraded intermediate estimates. Use forced mode as an execution-order control, not an estimator benchmark.",
        )
    )

    lines.extend(("", "### Effect of `ANALYZE`", ""))
    pre_by_query = {int(row["query_id"]): row for row in pre_analyze}
    post_by_query = {
        int(row["query_id"]): row
        for row in first_runs
        if row["canonical"] and row["optimizer_mode"] == "optimized"
    }
    for query_id in sorted(pre_by_query):
        pre = pre_by_query[query_id]
        post = post_by_query.get(query_id)
        if post is None:
            continue
        assoc_changed = pre["association_fingerprint"] != post["association_fingerprint"]
        orientation_changed = (
            pre["orientation_fingerprint"] != post["orientation_fingerprint"]
        )
        ordered_changed = pre["ordered_fingerprint"] != post["ordered_fingerprint"]
        lines.append(
            f"- Q{query_id}: association {'changed' if assoc_changed else 'unchanged'}; "
            f"orientation {'changed' if orientation_changed else 'unchanged'}; "
            f"ordered fingerprint {'changed' if ordered_changed else 'unchanged'}."
        )

    lines.extend(("", "## Refined transition brackets", ""))
    if transitions:
        lines.extend((
            "| Query | Axis | Last low | First high | Change | Probes | Evidence |",
            "|---:|---|---|---|---|---:|---|",
        ))
        for row in transitions:
            lines.append(
                f"| Q{row['query_id']} | {row['parameter_name']} | `{row['low_value']}` | "
                f"`{row['high_value']}` | {row['change_kind']} | {row['probe_count']} | "
                f"[low JSON]({row['low_raw_dir']}/explain.json) / "
                f"[low text]({row['low_raw_dir']}/explain.txt) / "
                f"[high JSON]({row['high_raw_dir']}/explain.json) / "
                f"[high text]({row['high_raw_dir']}/explain.txt) |"
            )
        lines.append("")
        lines.append(
            "Searches are endpoint-triggered and non-exhaustive: equal coarse endpoint plans do not prove that no re-entrant transition exists between them."
        )
    else:
        lines.append("No refinable coarse endpoints had different optimized fingerprints, or refinement was disabled.")

    q8 = validations.get("q8_negative_control", {"status": "not_run"})
    q8_control = first_run_by_key.get(
        (8, "case_nation_control", "optimized")
    ) or first_run_by_key.get((8, "case_nation_control", "forced"))
    q8_control_nation = None
    if q8_control is not None:
        q8_control_nation = json.loads(str(q8_control["parameters_json"])).get("case_nation")
    lines.extend(
        (
            "",
            "## Q8 CASE-nation negative control",
            "",
            f"- Status: `{q8.get('status')}`",
            f"- Result: {str(q8.get('note', 'Q8 was not selected')).rstrip('.')}.",
            f"- Alternate nation: `{q8_control_nation or 'not available'}`.",
            f"- Plan and complete audited cardinality vector invariant: "
            f"`{q8.get('plan_and_cardinality_invariant', False)}`.",
            "",
            "## ORQ transfer guidance",
            "",
            "DuckDB results describe logical row cardinalities over standard DBGEN data. ORQ's synthetic generator and oblivious operators have different distributions and cost drivers, so thresholds and physical build/probe choices are not directly portable.",
            "",
            "- **Q1:** ORQ draws `ShipDate` uniformly from 1–120 and filters at 60, giving about 50% logical validity rather than canonical Q1's very broad filter.",
            "- **Q3:** ORQ uses an approximately 20% segment filter, `OrderDate < 100` (about 82.5%), and `ShipDate > 100` (about 16.7%). Its dates are independent, unlike DBGEN's correlated order/ship dates.",
            "- **Q5:** ORQ's ten-value date interval retains about 8.3% of orders and its region retains about 20% of nations.",
            "- **Q8:** ORQ retains about 10% by exact synthetic part type, 8.3% by date, and 20% by region. The CASE nation is not a row filter.",
            "- **Q9:** ORQ's exact part-name predicate retains about 1/21, but the composite partsupp join retains an expected fraction of only about `4 / |Supplier|` of independently generated lineitems. Standard DBGEN makes that composite relationship valid, so Q9 order findings are not transferable until this retention is modeled.",
            "",
            "In ORQ, `filter` updates a secret validity mask while preserving physical vector length. `left.inner_join(right)` also assumes PK/FK direction, sorts obliviously, and may trim using public lengths. Evaluate every candidate ORQ order with both expected valid rows and physical secure-table length; do not copy DuckDB's hash build/probe orientation directly.",
            "",
            "## Artifact index",
            "",
            "- [`scenarios.csv`](scenarios.csv): one row per measured profile run",
            "- [`operators.csv`](operators.csv): flattened operators with estimated and actual cardinalities",
            "- [`joins.csv`](joins.csv): join inputs, fanout/retention, and pair selectivity",
            "- [`audit_counts.csv`](audit_counts.csv): authored-filter and logical-intermediate ground truth",
            "- [`transitions.csv`](transitions.csv): refined plan-change brackets",
            "- [`metadata.json`](metadata.json): complete run configuration",
        )
    )
    _write_text(output_dir / "report.md", "\n".join(lines))


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    status = run("status", "--porcelain")
    return {
        "git_commit": run("rev-parse", "HEAD"),
        "git_branch": run("branch", "--show-current"),
        "git_dirty": bool(status) if status is not None else None,
        "git_status_porcelain": status,
    }


def ensure_tpch_extension(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    installed_during_run = False
    try:
        connection.execute("LOAD tpch")
    except duckdb.Error:
        try:
            connection.execute("INSTALL tpch")
            installed_during_run = True
            connection.execute("LOAD tpch")
        except duckdb.Error as exc:
            raise RuntimeError(
                "DuckDB's tpch extension is unavailable. The first run needs network access; "
                "install it with `INSTALL tpch;` using this DuckDB version and retry."
            ) from exc
    description = connection.execute(
        """
SELECT installed, loaded, install_path, extension_version, install_mode, installed_from
FROM duckdb_extensions() WHERE extension_name='tpch'
"""
    ).fetchone()
    return {
        "installed_during_run": installed_during_run,
        "installed": description[0],
        "loaded": description[1],
        "install_path": description[2],
        "extension_version": description[3],
        "install_mode": description[4],
        "installed_from": description[5],
    }


def parse_queries(value: str) -> tuple[int, ...]:
    selected: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise argparse.ArgumentTypeError(f"invalid query range: {token}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(token))
    unsupported = selected.difference(SUPPORTED_QUERIES)
    if unsupported:
        raise argparse.ArgumentTypeError(
            "unsupported queries: " + ", ".join(str(item) for item in sorted(unsupported))
        )
    if not selected:
        raise argparse.ArgumentTypeError("select at least one query")
    return tuple(sorted(selected))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure DuckDB TPC-H selectivities and join-order changes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--scale-factor", type=float, default=0.1, help="DBGEN scale factor")
    parser.add_argument("--sweep-preset", choices=("legacy", "controlled-common"), default="legacy")
    parser.add_argument("--input-mode", choices=("direct", "materialized", "both"), default="both")
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--timing-repetitions", type=int, default=30)
    parser.add_argument("--max-transition-probes", type=int, default=512)
    parser.add_argument("--include-zero", action="store_true", help="separate zero-row boundary controls")
    parser.add_argument(
        "--queries",
        type=parse_queries,
        default=SUPPORTED_QUERIES,
        help="comma-separated query numbers/ranges drawn from 1,3,5,8,9",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="new or empty artifact directory"
    )
    parser.add_argument(
        "--database", type=Path, help="new DuckDB path (never overwrites an existing DB)"
    )
    parser.add_argument("--threads", type=int, default=1, help="DuckDB worker threads")
    parser.add_argument(
        "--repetitions", type=int, default=1, help="measured JSON EXPLAIN ANALYZE runs"
    )
    parser.add_argument(
        "--optimizer-mode",
        choices=("optimized", "forced", "both"),
        default="both",
        help="run normal optimization, forced SQL join order, or both",
    )
    parser.add_argument(
        "--skip-transition-refinement",
        action="store_true",
        help="do not bisect changed adjacent optimized plans",
    )
    return parser


def _prepare_paths(args: argparse.Namespace, repo_root: Path) -> tuple[Path, Path]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    sf_label = format(args.scale_factor, "g").replace(".", "p")
    output_dir = args.output_dir or (
        repo_root / "results" / "duckdb-tpch-selectivity" / f"{timestamp}-sf{sf_label}"
    )
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    database = args.database.resolve() if args.database else output_dir / "tpch.duckdb"
    if database.exists():
        raise RuntimeError(
            f"refusing to overwrite or mutate existing database: {database}. "
            "Choose a new --database path."
        )
    database.parent.mkdir(parents=True, exist_ok=True)
    return output_dir, database


def _validate_args(args: argparse.Namespace) -> None:
    if not math.isfinite(args.scale_factor) or args.scale_factor <= 0:
        raise ValueError("--scale-factor must be positive")
    if args.threads <= 0:
        raise ValueError("--threads must be positive")
    if args.repetitions <= 0:
        raise ValueError("--repetitions must be positive")
    if args.warmups < 0 or args.timing_repetitions <= 0:
        raise ValueError("warmups must be nonnegative and timing repetitions positive")
    if not 0 <= args.max_transition_probes <= 512:
        raise ValueError("--max-transition-probes must be between 0 and 512")
    if args.sweep_preset == "controlled-common":
        if duckdb.__version__ != "1.5.4" or args.threads != 1:
            raise ValueError("controlled-common requires DuckDB 1.5.4 and --threads 1")
        if args.optimizer_mode != "both":
            raise ValueError("controlled-common requires --optimizer-mode both for verified controls")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_args(args)
    if args.sweep_preset == "controlled-common":
        from duckdb_controlled import run
        return run(args, sys.modules[__name__])
    repo_root = Path(__file__).resolve().parents[1]
    output_dir, database = _prepare_paths(args, repo_root)
    started_at = datetime.now(timezone.utc)

    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": None,
        "duckdb_python_version": duckdb.__version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "scale_factor": args.scale_factor,
        "queries": list(args.queries),
        "threads": args.threads,
        "repetitions": args.repetitions,
        "optimizer_mode": args.optimizer_mode,
        "effective_disabled_optimizers": {
            "optimized": "",
            "forced": ",".join(FORCED_OPTIMIZERS),
        },
        "transition_refinement": not args.skip_transition_refinement,
        "database": str(database),
        "data_source": "DuckDB tpch extension DBGEN",
        "data_generated": True,
        **_git_metadata(repo_root),
    }
    _write_json(output_dir / "metadata.json", metadata)

    connection = duckdb.connect(str(database))
    try:
        connection.execute(f"SET threads = {args.threads}")
        connection.execute("SET enable_progress_bar = false")
        metadata["tpch_extension"] = ensure_tpch_extension(connection)
        connection.execute(f"CALL dbgen(sf = {args.scale_factor:.15g})")
        metadata["table_rows"] = {
            table: _count(connection, f"SELECT count(*) FROM {table}")
            for table in ("customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier")
        }
        metadata["duckdb_version_row"] = list(connection.execute("PRAGMA version").fetchone())
        metadata["optimizers"] = [row[0] for row in connection.execute("FROM duckdb_optimizers()").fetchall()]
        metadata["initial_disabled_optimizers"] = connection.execute(
            "SELECT current_setting('disabled_optimizers')"
        ).fetchone()[0]

        official_queries = {
            int(query_nr): str(sql)
            for query_nr, sql in connection.execute(
                "SELECT query_nr, query FROM tpch_queries()"
            ).fetchall()
            if int(query_nr) in args.queries
        }
        scenarios = build_scenarios(connection, args.queries)
        _write_json(
            output_dir / "scenario_definitions.json",
            [
                {
                    "query_id": scenario.query_id,
                    "name": scenario.name,
                    "family": scenario.family,
                    "order": scenario.order,
                    "description": scenario.description,
                    "parameters": scenario.parameters,
                    "canonical": scenario.canonical,
                    "negative_control": scenario.negative_control,
                }
                for scenario in scenarios
            ],
        )

        print("Capturing canonical plans before ANALYZE...", flush=True)
        pre_analyze = capture_pre_analyze_plans(connection, scenarios, output_dir)
        connection.execute("ANALYZE")
        metadata["analyze_status"] = "completed"
        metadata["analyze_completed_at_utc"] = datetime.now(timezone.utc).isoformat()

        print("Validating canonical ANSI templates...", flush=True)
        canonical_validation = validate_canonical_queries(
            connection, scenarios, official_queries, output_dir
        )

        print("Running independent selectivity and intermediate-count audits...", flush=True)
        audit_rows: list[dict[str, Any]] = []
        for scenario in scenarios:
            audit_rows.extend(audit_scenario(connection, scenario))
        validate_audit_monotonicity(audit_rows)

        modes = ("optimized", "forced") if args.optimizer_mode == "both" else (args.optimizer_mode,)
        scenario_rows: list[dict[str, Any]] = []
        operator_rows: list[dict[str, Any]] = []
        join_rows: list[dict[str, Any]] = []
        profile_summaries: dict[tuple[int, str, str], dict[str, Any]] = {}
        for scenario in scenarios:
            for mode in modes:
                print(f"Profiling Q{scenario.query_id} {scenario.name} [{mode}]...", flush=True)
                canonical_match = (
                    canonical_validation[scenario.query_id]["matches"] if scenario.canonical else None
                )
                run_scenarios, run_operators, run_joins, summary = profile_scenario(
                    connection,
                    scenario,
                    mode,
                    args.repetitions,
                    output_dir,
                    canonical_match,
                )
                scenario_rows.extend(run_scenarios)
                operator_rows.extend(run_operators)
                join_rows.extend(run_joins)
                profile_summaries[(scenario.query_id, scenario.name, mode)] = summary

        validations = validate_plan_controls(scenario_rows, operator_rows, join_rows, audit_rows)
        transitions: list[dict[str, Any]] = []
        if not args.skip_transition_refinement and "optimized" in modes:
            print("Refining endpoint-triggered plan transitions...", flush=True)
            transitions = refine_transitions(
                connection, scenarios, profile_summaries, output_dir
            )

        write_csv(
            output_dir / "scenarios.csv",
            scenario_rows,
            SCENARIO_FIELDS,
        )
        write_csv(
            output_dir / "operators.csv",
            operator_rows,
            OPERATOR_FIELDS,
        )
        write_csv(
            output_dir / "joins.csv",
            join_rows,
            JOIN_FIELDS,
        )
        write_csv(
            output_dir / "audit_counts.csv",
            audit_rows,
            AUDIT_FIELDS,
        )
        write_csv(
            output_dir / "transitions.csv",
            transitions,
            TRANSITION_FIELDS,
        )

        metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        metadata["elapsed_seconds"] = (
            datetime.fromisoformat(metadata["finished_at_utc"]) - started_at
        ).total_seconds()
        metadata["scenario_count"] = len(scenarios)
        metadata["profile_run_count"] = len(scenario_rows)
        metadata["transition_count"] = len(transitions)
        metadata["validations"] = validations
        _write_json(output_dir / "metadata.json", metadata)
        generate_report(
            output_dir,
            metadata,
            scenarios,
            scenario_rows,
            operator_rows,
            audit_rows,
            transitions,
            canonical_validation,
            pre_analyze,
            validations,
        )
    except Exception as exc:
        metadata["failed_at_utc"] = datetime.now(timezone.utc).isoformat()
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(output_dir / "metadata.json", metadata)
        raise
    finally:
        connection.close()

    print(f"Finished. Report: {output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
