"""Focused tests for the DuckDB TPC-H selectivity experiment helpers.

These tests deliberately use a small, in-memory schema.  They exercise DuckDB's
real JSON EXPLAIN formats without downloading or loading the TPC-H extension.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import duckdb


MODULE_PATH = Path(__file__).resolve().parents[1] / "analyze_duckdb_tpch.py"
SPEC = importlib.util.spec_from_file_location("analyze_duckdb_tpch_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - importlib guard
    raise ImportError(f"cannot load experiment module from {MODULE_PATH}")
experiment = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = experiment
SPEC.loader.exec_module(experiment)


class PlanHelperTests(unittest.TestCase):
    """Exercise parsing, flattening, and fingerprints on an actual join plan."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = duckdb.connect(":memory:")
        cls.connection.execute("CREATE TABLE a(id INTEGER, payload INTEGER)")
        cls.connection.execute("CREATE TABLE b(id INTEGER, a_id INTEGER)")
        cls.connection.execute("CREATE TABLE c(id INTEGER, b_id INTEGER)")
        cls.connection.execute(
            "INSERT INTO a VALUES (1, 10), (2, 20), (3, 30)"
        )
        cls.connection.execute(
            "INSERT INTO b VALUES (10, 1), (20, 2), (30, 2)"
        )
        cls.connection.execute(
            "INSERT INTO c VALUES (100, 10), (200, 20), (300, 30)"
        )
        cls.filtered_query = """
            SELECT a.id AS a_id, b.id AS b_id, c.id AS c_id
            FROM a
            JOIN b ON b.a_id = a.id
            JOIN c ON c.b_id = b.id
            WHERE a.payload >= 20
        """

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    def explain_json(self, query: str, *, analyze: bool = False):
        options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
        rows = self.connection.execute(f"EXPLAIN ({options}) {query}").fetchall()
        values = {key: value for key, value in rows}
        expected_key = "analyzed_plan" if analyze else "physical_plan"
        self.assertIn(expected_key, values)
        return experiment.parse_json_explain(values[expected_key])

    def test_parse_and_flatten_static_three_table_plan(self) -> None:
        with experiment.optimizer_mode(self.connection, "forced"):
            root = self.explain_json(self.filtered_query)

        self.assertIsInstance(root, (dict, list))
        rows = experiment.flatten_plan(
            root, query_id="qtest", scenario="three_table", mode="forced"
        )

        # Projection/filter nodes may be folded away, but the two joins and
        # three scans must always remain.
        self.assertGreaterEqual(len(rows), 5)
        required = {
            "query_id",
            "scenario",
            "mode",
            "node_id",
            "parent_id",
            "operator_name",
            "estimated_cardinality",
            "actual_cardinality",
        }
        for row in rows:
            self.assertTrue(required.issubset(row), row)
            self.assertEqual("qtest", row["query_id"])
            self.assertEqual("three_table", row["scenario"])
            self.assertEqual("forced", row["mode"])

        names = [str(row["operator_name"]).strip().upper() for row in rows]
        self.assertEqual(2, sum(name.endswith("JOIN") for name in names))
        self.assertGreaterEqual(sum("SCAN" in name for name in names), 3)
        self.assertTrue(
            all(row["actual_cardinality"] is None for row in rows),
            "A non-ANALYZE plan must not invent actual cardinalities",
        )
        self.assertTrue(
            any(row["estimated_cardinality"] is not None for row in rows)
        )

    def test_flatten_analyzed_plan_uses_operator_cardinality(self) -> None:
        with experiment.optimizer_mode(self.connection, "forced"):
            root = self.explain_json(self.filtered_query, analyze=True)

        rows = experiment.flatten_plan(
            root, query_id="qtest", scenario="analyzed", mode="forced"
        )
        joins = [
            row
            for row in rows
            if str(row["operator_name"]).strip().upper().endswith("JOIN")
        ]
        names = {
            str(row["operator_name"]).strip().upper()
            for row in rows
        }

        self.assertEqual(2, len(joins))
        self.assertEqual([2, 2], [row["actual_cardinality"] for row in joins])
        self.assertTrue(
            all(row["estimated_cardinality"] is not None for row in joins)
        )
        self.assertNotIn("UNKNOWN", names)
        self.assertNotIn("EXPLAIN_ANALYZE", names)

    def test_fingerprints_separate_association_from_child_orientation(self) -> None:
        abc = """
            SELECT count(*)
            FROM a
            JOIN b ON b.a_id = a.id
            JOIN c ON c.b_id = b.id
        """
        bac = """
            SELECT count(*)
            FROM b
            JOIN a ON b.a_id = a.id
            JOIN c ON c.b_id = b.id
        """
        cba = """
            SELECT count(*)
            FROM c
            JOIN b ON c.b_id = b.id
            JOIN a ON b.a_id = a.id
        """
        with experiment.optimizer_mode(self.connection, "forced"):
            abc_root = self.explain_json(abc)
            bac_root = self.explain_json(bac)
            cba_root = self.explain_json(cba)

        abc_association, abc_ordered = experiment.plan_fingerprints(abc_root)
        bac_association, bac_ordered = experiment.plan_fingerprints(bac_root)
        cba_association, _ = experiment.plan_fingerprints(cba_root)
        abc_orientation = experiment.plan_orientation_fingerprint(abc_root)
        bac_orientation = experiment.plan_orientation_fingerprint(bac_root)

        for fingerprint in (abc_association, abc_ordered):
            self.assertIsInstance(fingerprint, str)
            self.assertTrue(fingerprint)
            lowered = fingerprint.lower()
            for table in ("a", "b", "c"):
                self.assertIn(table, lowered)

        # The first pair is {a,b} in both plans, but its probe/build orientation
        # is reversed.  A different first pair ({b,c}) changes association too.
        self.assertEqual(abc_association, bac_association)
        self.assertNotEqual(abc_ordered, bac_ordered)
        self.assertNotEqual(abc_orientation, bac_orientation)
        self.assertNotEqual(abc_association, cba_association)

    def test_static_and_analyzed_plan_have_the_same_fingerprints(self) -> None:
        with experiment.optimizer_mode(self.connection, "forced"):
            static_root = self.explain_json(self.filtered_query)
            analyzed_root = self.explain_json(self.filtered_query, analyze=True)

        self.assertEqual(
            experiment.plan_fingerprints(static_root),
            experiment.plan_fingerprints(analyzed_root),
        )

    def test_association_fingerprint_ignores_join_metadata(self) -> None:
        def plan(join_type: str, conditions: str):
            def scan(table: str):
                return {
                    "name": "SEQ_SCAN",
                    "extra_info": {"Table": f"memory.main.{table}"},
                    "children": [],
                }

            return {
                "name": "HASH_JOIN",
                "extra_info": {
                    "Join Type": join_type,
                    "Conditions": conditions,
                },
                "children": [scan("a"), scan("b")],
            }

        inner_association, inner_ordered = experiment.plan_fingerprints(
            plan("INNER", "a_id = id")
        )
        left_association, left_ordered = experiment.plan_fingerprints(
            plan("LEFT", "other_id = id")
        )
        inner_metadata = experiment.plan_metadata_fingerprint(
            plan("INNER", "a_id = id")
        )
        left_metadata = experiment.plan_metadata_fingerprint(
            plan("LEFT", "other_id = id")
        )

        self.assertEqual(inner_association, left_association)
        self.assertNotEqual(inner_ordered, left_ordered)
        self.assertNotEqual(inner_metadata, left_metadata)
        self.assertEqual(
            experiment.plan_orientation_fingerprint(plan("INNER", "a_id = id")),
            experiment.plan_orientation_fingerprint(plan("LEFT", "other_id = id")),
        )

    def test_single_nation_scan_is_not_given_a_q8_role(self) -> None:
        root = {
            "name": "SEQ_SCAN",
            "extra_info": {
                "Table": "memory.main.nation",
                "Projections": ["n_name", "n_nationkey"],
            },
            "children": [],
        }

        association, ordered = experiment.plan_fingerprints(root)

        self.assertEqual("nation", association)
        self.assertEqual("nation", ordered)


class MetricHelperTests(unittest.TestCase):
    def test_safe_ratio_handles_zero_denominators(self) -> None:
        self.assertEqual(2.0, experiment.safe_ratio(4, 2))
        self.assertEqual(0.0, experiment.safe_ratio(0, 5))
        self.assertIsNone(experiment.safe_ratio(5, 0))
        self.assertIsNone(experiment.safe_ratio(0, 0))

    def test_cardinality_errors_for_nonzero_values(self) -> None:
        errors = experiment.cardinality_errors(10, 5)
        self.assertAlmostEqual(1.0, errors["relative_error"])
        self.assertAlmostEqual(2.0, errors["q_error"])
        self.assertAlmostEqual(11.0 / 6.0, errors["smoothed_q_error"])

    def test_cardinality_errors_make_zero_cases_explicit(self) -> None:
        both_zero = experiment.cardinality_errors(0, 0)
        self.assertIsNone(both_zero["relative_error"])
        self.assertEqual(1.0, both_zero["q_error"])
        self.assertEqual(1.0, both_zero["smoothed_q_error"])

        estimate_only = experiment.cardinality_errors(5, 0)
        self.assertIsNone(estimate_only["relative_error"])
        self.assertTrue(math.isinf(estimate_only["q_error"]))
        self.assertEqual(6.0, estimate_only["smoothed_q_error"])

        actual_only = experiment.cardinality_errors(0, 5)
        self.assertEqual(-1.0, actual_only["relative_error"])
        self.assertTrue(math.isinf(actual_only["q_error"]))
        self.assertEqual(6.0, actual_only["smoothed_q_error"])

    @staticmethod
    def join_plan(left: int, right: int, output: int):
        def scan(table: str, cardinality: int):
            return {
                "operator_name": "SEQ_SCAN",
                "operator_type": "TABLE_SCAN",
                "operator_cardinality": cardinality,
                "extra_info": {
                    "Table": f"memory.main.{table}",
                    "Estimated Cardinality": str(cardinality),
                },
                "children": [],
            }

        return {
            "operator_name": "HASH_JOIN",
            "operator_type": "HASH_JOIN",
            "operator_cardinality": output,
            "extra_info": {
                "Join Type": "INNER",
                "Conditions": "id = id",
                "Estimated Cardinality": str(output),
            },
            "children": [scan("a", left), scan("b", right)],
        }

    def test_join_metrics_report_fanout_against_both_inputs(self) -> None:
        rows = experiment._join_rows(
            self.join_plan(left=3, right=2, output=6),
            run_id="fanout",
            query_id=0,
            scenario="unit",
            mode="optimized",
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(2.0, row["output_per_left"])
        self.assertEqual(3.0, row["output_per_right"])
        self.assertEqual(1.0, row["pair_selectivity"])

    def test_join_metrics_leave_zero_input_ratios_undefined(self) -> None:
        rows = experiment._join_rows(
            self.join_plan(left=0, right=5, output=0),
            run_id="empty",
            query_id=0,
            scenario="unit",
            mode="optimized",
        )

        row = rows[0]
        self.assertIsNone(row["output_per_left"])
        self.assertEqual(0.0, row["output_per_right"])
        self.assertIsNone(row["pair_selectivity"])


class ArtifactHelperTests(unittest.TestCase):
    def test_frozen_canonical_manifest_reproduces_plan_fingerprints(self) -> None:
        plan_dir = MODULE_PATH.parent / "tpch-duckdb-plans"
        manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual("1.5.4", manifest["source"]["duckdb_version"])
        for query, entry in manifest["queries"].items():
            raw_plan = json.loads(
                (plan_dir / entry["explain_json"]).read_text(encoding="utf-8")
            )
            association, ordered = experiment.plan_fingerprints(raw_plan)
            self.assertEqual(entry["association_tree"], association, query)
            self.assertEqual(entry["ordered_tree"], ordered, query)
            self.assertEqual(
                entry["association_fingerprint_id"],
                experiment._fingerprint_id(association),
                query,
            )

    def test_write_csv_preserves_preferred_order_and_unions_row_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.csv"
            experiment.write_csv(
                path,
                [
                    {"second": 2, "extra": "x"},
                    {"first": 1, "later": True},
                ],
                ("first", "second"),
            )

            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(["first", "second", "extra", "later"], reader.fieldnames)
        self.assertEqual("2", rows[0]["second"])
        self.assertEqual("x", rows[0]["extra"])
        self.assertEqual("1", rows[1]["first"])
        self.assertEqual("True", rows[1]["later"])

    def test_write_csv_emits_a_header_for_an_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.csv"
            experiment.write_csv(path, [], experiment.TRANSITION_FIELDS)
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(list(experiment.TRANSITION_FIELDS), reader.fieldnames)
        self.assertEqual([], rows)

    def test_generate_report_writes_core_evidence_and_relative_links(self) -> None:
        metadata = {
            "finished_at_utc": "2026-01-01T00:00:00+00:00",
            "duckdb_python_version": "1.5.4",
            "scale_factor": 0.01,
            "threads": 1,
            "repetitions": 1,
            "database": "/tmp/example.duckdb",
            "git_commit": "abc123",
            "git_dirty": False,
        }
        scenario_rows = [
            {
                "run_id": "q01.canonical.optimized.r1",
                "query_id": 1,
                "scenario": "canonical",
                "optimizer_mode": "optimized",
                "repetition": 1,
                "canonical": True,
                "association_fingerprint": "lineitem",
                "ordered_fingerprint": "lineitem",
                "orientation_fingerprint": "lineitem",
                "max_join_actual_rows": None,
                "profile_latency_seconds": 0.01,
                "raw_dir": "plans/q01/canonical/optimized",
            }
        ]
        operator_rows = [
            {
                "run_id": "q01.canonical.optimized.r1",
                "scenario": "canonical",
                "operator_name": "SEQ_SCAN",
                "relation_set": "lineitem",
                "estimated_cardinality": 5,
                "actual_cardinality": 9,
                "q_error": 2.0,
            }
        ]
        audit_rows = [
            {
                "scenario_id": "q01.canonical",
                "audit_kind": "base_predicate",
                "scope": "lineitem",
                "audit_id": "lineitem_shipdate",
                "numerator": 9,
                "denominator": 10,
                "value": 0.9,
            }
        ]
        canonical_validation = {
            1: {
                "matches": True,
                "custom_rows": 4,
                "custom_checksum": "0123456789abcdef",
            }
        }
        pre_analyze = [
            {
                "query_id": 1,
                "association_fingerprint": "lineitem",
                "ordered_fingerprint": "lineitem",
                "orientation_fingerprint": "lineitem",
            }
        ]
        transitions = [
            {
                "query_id": 9,
                "parameter_name": "partkey_threshold",
                "low_value": "14",
                "high_value": "15",
                "change_kind": "orientation",
                "probe_count": 4,
                "low_raw_dir": "transitions/q09/14",
                "high_raw_dir": "transitions/q09/15",
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            experiment.generate_report(
                output_dir,
                metadata,
                [],
                scenario_rows,
                operator_rows,
                audit_rows,
                transitions,
                canonical_validation,
                pre_analyze,
                {},
            )
            report = (output_dir / "report.md").read_text(encoding="utf-8")

        self.assertIn("## Canonical validation", report)
        self.assertIn("q01.canonical", report)
        self.assertIn("finite q-error", report)
        self.assertIn(
            "[plans/q01/canonical/optimized](plans/q01/canonical/optimized)",
            report,
        )
        self.assertIn(
            "[low JSON](transitions/q09/14/explain.json)", report
        )
        self.assertIn(
            "[high text](transitions/q09/15/explain.txt)", report
        )
        self.assertIn("## ORQ transfer guidance", report)


class OptimizerModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")

    def tearDown(self) -> None:
        self.connection.close()

    def disabled_optimizers(self) -> str:
        return self.connection.execute(
            "SELECT current_setting('disabled_optimizers')"
        ).fetchone()[0]

    def test_forced_mode_restores_setting_after_exception(self) -> None:
        try:
            self.connection.execute("SET disabled_optimizers = 'filter_pushdown'")
            before = self.disabled_optimizers()

            with self.assertRaisesRegex(RuntimeError, "deliberate"):
                with experiment.optimizer_mode(self.connection, "forced"):
                    disabled = {
                        item.strip()
                        for item in self.disabled_optimizers().split(",")
                    }
                    self.assertIn("join_order", disabled)
                    self.assertIn("build_side_probe_side", disabled)
                    raise RuntimeError("deliberate context-manager failure")

            self.assertEqual(before, self.disabled_optimizers())
        finally:
            self.connection.execute("SET disabled_optimizers = ''")

    def test_optimized_mode_temporarily_enables_all_optimizers(self) -> None:
        try:
            self.connection.execute("SET disabled_optimizers = 'filter_pushdown'")
            before = self.disabled_optimizers()
            with experiment.optimizer_mode(self.connection, "optimized"):
                self.assertEqual("", self.disabled_optimizers())
            self.assertEqual(before, self.disabled_optimizers())
        finally:
            self.connection.execute("SET disabled_optimizers = ''")


class CliHelperTests(unittest.TestCase):
    def test_parse_queries_accepts_lists_and_supported_ranges(self) -> None:
        self.assertEqual((1, 3, 5, 8, 9), experiment.parse_queries("9,1,3,5,8-9"))

    def test_parse_queries_rejects_unsupported_query_numbers(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            experiment.parse_queries("1-3")

    def test_validate_args_rejects_nonfinite_scale_factors(self) -> None:
        parser = experiment.build_parser()
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                args = parser.parse_args([f"--scale-factor={value}"])
                with self.assertRaisesRegex(ValueError, "scale-factor"):
                    experiment._validate_args(args)

    def test_prepare_paths_refuses_nonempty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "results"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("preserve me", encoding="utf-8")
            args = experiment.build_parser().parse_args(
                ["--scale-factor", "0.01", "--output-dir", str(output)]
            )

            with self.assertRaisesRegex(RuntimeError, "not empty"):
                experiment._prepare_paths(args, root)

            self.assertEqual("preserve me", marker.read_text(encoding="utf-8"))


@unittest.skipUnless(
    os.environ.get("DUCKDB_TPCH_E2E") == "1",
    "set DUCKDB_TPCH_E2E=1 when DuckDB's tpch extension is installed",
)
class EndToEndTests(unittest.TestCase):
    def test_sf001_all_queries_produce_consistent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "experiment"
            result = experiment.main(
                (
                    "--scale-factor", "0.01",
                    "--output-dir", str(output_dir),
                    "--skip-transition-refinement",
                )
            )

            self.assertEqual(0, result)
            metadata = json.loads(
                (output_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(18, metadata["scenario_count"])
            self.assertEqual(36, metadata["profile_run_count"])
            self.assertEqual(
                "passed", metadata["validations"]["q8_negative_control"]["status"]
            )
            for query_id in experiment.SUPPORTED_QUERIES:
                validation = json.loads(
                    (
                        output_dir
                        / "canonical_validation"
                        / f"q{query_id:02d}"
                        / "validation.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertTrue(validation["matches"], f"Q{query_id}")
            for artifact in (
                "report.md", "scenarios.csv", "operators.csv", "joins.csv",
                "audit_counts.csv", "transitions.csv",
            ):
                self.assertTrue((output_dir / artifact).is_file(), artifact)


class ValidationHelperTests(unittest.TestCase):
    @staticmethod
    def forced_q3_scenario_row():
        return {
            "run_id": "q03.unit.forced.r1",
            "query_id": 3,
            "optimizer_mode": "forced",
            "repetition": 1,
            "static_analyzed_fingerprint_match": True,
        }

    @staticmethod
    def join_row(left: str, right: str, output: str):
        return {
            "run_id": "q03.unit.forced.r1",
            "left_relations": left,
            "right_relations": right,
            "output_relations": output,
        }

    def test_forced_order_validation_rejects_reversed_right_deep_tree(self) -> None:
        # These unordered output sets look like the declared Q3 prefixes, but
        # both joins have the wrong left/right orientation.
        joins = [
            self.join_row("orders", "customer", "customer|orders"),
            self.join_row(
                "lineitem", "customer|orders", "customer|lineitem|orders"
            ),
        ]

        with self.assertRaisesRegex(RuntimeError, "left-deep order"):
            experiment.validate_plan_controls(
                [self.forced_q3_scenario_row()], [], joins, []
            )

    def test_forced_order_validation_accepts_declared_q3_tree(self) -> None:
        joins = [
            self.join_row("customer", "orders", "customer|orders"),
            self.join_row(
                "customer|orders", "lineitem", "customer|lineitem|orders"
            ),
        ]

        result = experiment.validate_plan_controls(
            [self.forced_q3_scenario_row()], [], joins, []
        )

        self.assertEqual("not_run", result["q8_negative_control"]["status"])

    @staticmethod
    def q8_scenario_row(
        scenario: str, checksum: str, *, contributing_control: bool = True
    ):
        parameters = {"case_nation": "BRAZIL"}
        if scenario == "case_nation_control":
            parameters = {
                "case_nation": "CANADA",
                "control_has_contributing_rows": contributing_control,
            }
        return {
            "run_id": f"q08.{scenario}.optimized.r1",
            "query_id": 8,
            "scenario": scenario,
            "optimizer_mode": "optimized",
            "repetition": 1,
            "static_analyzed_fingerprint_match": True,
            "association_fingerprint": "same-association",
            "ordered_fingerprint": "same-order",
            "result_checksum": checksum,
            "parameters_json": json.dumps(parameters),
        }

    @staticmethod
    def q8_audit_rows():
        rows = []
        values = {
            "part_type": 0.01,
            "orders_date": 0.30,
            "region_name": 0.20,
            "regional_customers": 0.20,
            "part_lineitems": 42,
            "full_preaggregate": 7,
        }
        for scenario in ("canonical", "case_nation_control"):
            for audit_id, value in values.items():
                rows.append(
                    {
                        "scenario_id": f"q08.{scenario}",
                        "query_id": 8,
                        "audit_id": audit_id,
                        "value": value,
                    }
                )
        return rows

    @staticmethod
    def q8_join_rows():
        return [
            {
                "run_id": f"q08.{scenario}.optimized.r1",
                "node_id": "0.0",
                "actual_cardinality": 7,
            }
            for scenario in ("canonical", "case_nation_control")
        ]

    def test_q8_contributing_control_must_change_the_result(self) -> None:
        scenarios = [
            self.q8_scenario_row("canonical", "same"),
            self.q8_scenario_row("case_nation_control", "same"),
        ]

        with self.assertRaisesRegex(RuntimeError, "result|checksum|change"):
            experiment.validate_plan_controls(
                scenarios, [], self.q8_join_rows(), self.q8_audit_rows()
            )

    def test_q8_control_passes_only_with_invariant_counts_and_changed_result(self) -> None:
        scenarios = [
            self.q8_scenario_row("canonical", "brazil"),
            self.q8_scenario_row("case_nation_control", "canada"),
        ]

        result = experiment.validate_plan_controls(
            scenarios, [], self.q8_join_rows(), self.q8_audit_rows()
        )

        check = result["q8_negative_control"]
        self.assertEqual("passed", check["status"])
        self.assertTrue(check["plan_and_cardinality_invariant"])
        self.assertTrue(check["result_changed"])

    def test_q8_control_without_contributing_rows_is_explicitly_inconclusive(self) -> None:
        scenarios = [
            self.q8_scenario_row("canonical", "same"),
            self.q8_scenario_row(
                "case_nation_control", "same", contributing_control=False
            ),
        ]

        result = experiment.validate_plan_controls(
            scenarios, [], self.q8_join_rows(), self.q8_audit_rows()
        )

        self.assertEqual("inconclusive", result["q8_negative_control"]["status"])


class TransitionHelperTests(unittest.TestCase):
    def test_change_classification_separates_orientation_and_metadata(self) -> None:
        orientation = experiment.classify_plan_change(
            "same", "same", "left", "right", "same-meta", "same-meta"
        )
        detail = experiment.classify_plan_change(
            "same", "same", "same-orientation", "same-orientation", "inner", "left"
        )
        combined = experiment.classify_plan_change(
            "same", "same", "left", "right", "inner", "left"
        )

        self.assertEqual("orientation", orientation["change_kind"])
        self.assertTrue(orientation["orientation_changed"])
        self.assertFalse(orientation["ordered_detail_changed"])
        self.assertEqual("ordered_detail", detail["change_kind"])
        self.assertFalse(detail["orientation_changed"])
        self.assertTrue(detail["ordered_detail_changed"])
        self.assertEqual(
            "orientation_and_ordered_detail", combined["change_kind"]
        )

    def test_refinement_bisects_boundary_and_writes_each_probe(self) -> None:
        low = experiment.Scenario(
            9,
            "low",
            "partkey_fraction",
            0,
            "low endpoint",
            {"threshold": 10, "part_predicate": "p.p_partkey <= 10"},
        )
        high = experiment.Scenario(
            9,
            "high",
            "partkey_fraction",
            1,
            "high endpoint",
            {"threshold": 20, "part_predicate": "p.p_partkey <= 20"},
        )
        summaries = {
            (9, "low", "optimized"): {
                "scenario": low,
                "static_fingerprints": ("association-low", "ordered-low"),
                "static_orientation_fingerprint": "orientation-low",
                "static_metadata_fingerprint": "metadata-low",
                "raw_dir": "plans/q09/low/optimized",
            },
            (9, "high", "optimized"): {
                "scenario": high,
                "static_fingerprints": ("association-high", "ordered-high"),
                "static_orientation_fingerprint": "orientation-high",
                "static_metadata_fingerprint": "metadata-high",
                "raw_dir": "plans/q09/high/optimized",
            },
        }

        def fingerprints(root):
            value = int(root["threshold"])
            suffix = "low" if value < 15 else "high"
            return (f"association-{suffix}", f"ordered-{suffix}")

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with (
                mock.patch.object(
                    experiment,
                    "query_sql",
                    side_effect=lambda scenario: str(scenario.parameters["threshold"]),
                ),
                mock.patch.object(
                    experiment,
                    "explain_json",
                    side_effect=lambda _connection, sql, **_kwargs: {
                        "threshold": int(sql)
                    },
                ),
                mock.patch.object(
                    experiment,
                    "explain_text",
                    return_value="synthetic plan\n",
                ),
                mock.patch.object(
                    experiment, "plan_fingerprints", side_effect=fingerprints
                ),
                mock.patch.object(
                    experiment,
                    "plan_orientation_fingerprint",
                    side_effect=lambda root: (
                        "orientation-low"
                        if int(root["threshold"]) < 15
                        else "orientation-high"
                    ),
                ),
                mock.patch.object(
                    experiment,
                    "plan_metadata_fingerprint",
                    side_effect=lambda root: (
                        "metadata-low"
                        if int(root["threshold"]) < 15
                        else "metadata-high"
                    ),
                ),
                mock.patch.object(
                    experiment,
                    "optimizer_mode",
                    side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
                ),
                mock.patch.object(
                    experiment,
                    "_transition_selectivity",
                    side_effect=lambda _connection, scenario: {
                        "part": scenario.parameters["threshold"] / 100.0
                    },
                ),
            ):
                transitions = experiment.refine_transitions(
                    object(), [low, high], summaries, output_dir
                )

            probe_root = output_dir / "transitions/q09/partkey_fraction"
            probe_dirs = sorted(path.name for path in probe_root.iterdir())
            for probe_dir in probe_root.iterdir():
                self.assertEqual(
                    {"query.sql", "explain.json", "explain.txt"},
                    {path.name for path in probe_dir.iterdir()},
                )

        self.assertEqual(1, len(transitions))
        transition = transitions[0]
        self.assertEqual("14", transition["low_value"])
        self.assertEqual("15", transition["high_value"])
        self.assertEqual("association", transition["change_kind"])
        self.assertEqual(4, transition["probe_count"])
        self.assertEqual(["12", "13", "14", "15"], probe_dirs)
        self.assertEqual(
            {"part": 0.14},
            json.loads(transition["low_realized_selectivity_json"]),
        )

if __name__ == "__main__":
    unittest.main()
