#!/usr/bin/env python3

import importlib.util
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


reporter = load_module(
    "report_tpch_plain_and_3pc", REPO_ROOT / "scripts" / "report_tpch_plain_and_3pc.py"
)
runner = load_module("run_experiment_for_tpch_test", REPO_ROOT / "scripts" / "run_experiment.py")


class ReportTests(unittest.TestCase):
    def test_statistics_and_concatenated_json(self):
        self.assertEqual(reporter.calculate_statistics([1, 2, 3])["median"], 2.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text('{"first": 1}\n{"second": 2}\n', encoding="utf-8")
            self.assertEqual(
                reporter.load_json_documents(path), [{"first": 1}, {"second": 2}]
            )

    def test_log_parser_extracts_operator_profile_bytes_and_rtt(self):
        log = """
Baseline ping latencies (before WAN simulation):
  node1: 0.250 ms
Ping latencies after enabling WAN simulation:
  node1: 13.500 ms
[QUERY_PLAN] query=q5 variant=duckdb-canonical
[=SW]            Start
[TABLE_JOIN] L=10 R=20 k=1 a=2
[TABLE_SORT] RS k=2 n=32
[TABLE_AGG] k=1 n=32 a=2
[PRIV_DIV] n=4
[=PROFILE]       table.sort 0.400000 sec
P0 Total 100
P1 Total 200
[=SW]            Start
[TABLE_JOIN] L=10 R=20 k=1 a=2
[=PROFILE]       table.sort 0.600000 sec
P0 Total 300
P1 Total 400
"""
        parsed = reporter.parse_log(log)
        self.assertEqual(len(parsed["operator_events"]), 5)
        self.assertEqual(parsed["operator_events"][0]["left_rows"], 10)
        self.assertEqual(parsed["operator_events"][-1]["repetition"], 1)
        self.assertEqual(parsed["operator_profile_seconds"]["table.sort"]["mean"], 0.5)
        self.assertEqual(parsed["communicator_bytes_sent"]["party_0"]["mean"], 200.0)
        self.assertEqual(
            parsed["network_rtt_ms"]["simulated_wan"]["node1"]["mean"], 13.5
        )
        self.assertEqual(
            parsed["query_plan_markers"][0]["variant"], "duckdb-canonical"
        )

    def test_frozen_plan_catalog_and_cpp_targets_match(self):
        catalog = reporter.load_plan_catalog()
        derived = catalog["variants"]["duckdb-canonical"]["queries"]
        self.assertEqual(catalog["duckdb_source"]["duckdb_version"], "1.5.4")
        self.assertEqual(
            derived["q5"]["association_tree"],
            "join(join(join(join(customer,join(nation,region)),orders),lineitem),supplier)",
        )
        self.assertEqual(derived["q1"]["translation_status"], "no-op-control")
        for query in reporter.QUERY_PLANS:
            wrapper = REPO_ROOT / "bench" / "queries" / "tpch" / f"{query}_duckdb.cpp"
            self.assertTrue(wrapper.exists())
            self.assertIn(
                "TPCH_DUCKDB_CANONICAL_PLAN", wrapper.read_text(encoding="utf-8")
            )
            implementation = (
                REPO_ROOT / "bench" / "queries" / "tpch" / f"{query}.cpp"
            ).read_text(encoding="utf-8")
            association_without_cpp_string_breaks = implementation.replace('"\n    "', "")
            self.assertIn(
                derived[query]["association_tree"], association_without_cpp_string_breaks
            )

    def test_report_builds_successful_run_and_marks_missing_matrix_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_dir = output_dir / "plaintext-1pc" / "same"
            run_dir.mkdir(parents=True)
            profile = {
                "config": {"protocol": 1, "setting": "same", "wan_sim": "off"},
                "raw_data": [
                    {"Filters": 1.0, "Overall": 2.0},
                    {"Filters": 2.0, "Overall": 3.0},
                    {"Filters": 3.0, "Overall": 4.0},
                ],
                "aggregated_data": {},
            }
            (run_dir / "q1.json").write_text(json.dumps(profile), encoding="utf-8")
            (run_dir / "q1.log").write_text(
                "\n".join([
                    "[=SW] Start",
                    "[TABLE_SORT] RS k=2 n=16",
                    "P0 Total 0",
                    "[=SW] Start",
                    "[TABLE_SORT] RS k=2 n=16",
                    "P0 Total 0",
                    "[=SW] Start",
                    "[TABLE_SORT] RS k=2 n=16",
                    "P0 Total 0",
                ]),
                encoding="utf-8",
            )

            data = reporter.build_report_data(output_dir, 0.1, 16, 3, "node", "simulated")
            q1 = next(
                run for run in data["runs"]
                if run["mode"] == "plaintext-1pc/same" and run["query"] == "q1"
            )
            self.assertEqual(q1["status"], "success")
            self.assertEqual(q1["stage_statistics_seconds"]["Overall"]["mean"], 3.0)
            self.assertEqual(len(data["runs"]), 15)
            self.assertEqual(len(data["failures"]), 14)
            markdown = reporter.render_markdown(data)
            self.assertIn("fixed C++ plans", markdown)
            self.assertIn("Observed operator trace", markdown)

    def test_paired_report_computes_speedup_and_communication_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            for mode in (
                "plaintext-1pc/same",
                "secure-3pc/lan",
                "secure-3pc/wan-real",
            ):
                run_dir = output_dir / mode
                run_dir.mkdir(parents=True)
                for query in reporter.QUERY_PLANS:
                    for suffix, overall, byte_count, marker in (
                        ("", 4.0, 400, "original-orq"),
                        ("-duckdb", 2.0, 200, "duckdb-canonical"),
                    ):
                        association = ""
                        if suffix:
                            frozen = reporter.load_plan_catalog()["variants"][
                                "duckdb-canonical"
                            ]["queries"][query]["association_tree"]
                            association = f" association={frozen}"
                        profile = {
                            "config": {"exp_args": ["--tpch-seed=20260818"]},
                            "raw_data": [{"Overall": overall}] * 3,
                        }
                        (run_dir / f"{query}{suffix}.json").write_text(
                            json.dumps(profile), encoding="utf-8"
                        )
                        (run_dir / f"{query}{suffix}.log").write_text(
                            "\n".join(
                                [
                                    f"[QUERY_PLAN] query={query} variant={marker}{association}",
                                    "[=SW] Start",
                                    f"P0 Total {byte_count}",
                                ]
                                * 3
                            ),
                            encoding="utf-8",
                        )

            data = reporter.build_report_data(
                output_dir, 0.1, 16, 3, "node", "real", "both", 20260818
            )
            self.assertEqual(data["benchmark"]["expected_executions"], 90)
            self.assertEqual(data["benchmark"]["profiled_executions"], 90)
            self.assertEqual(data["benchmark"]["successful_executions"], 90)
            self.assertEqual(data["failures"], [])
            q5_lan = next(
                item for item in data["comparisons"]
                if item["mode"] == "secure-3pc/lan" and item["query"] == "q5"
            )
            self.assertEqual(q5_lan["runtime_speedup_original_over_duckdb"], 2.0)
            self.assertEqual(q5_lan["communication_ratio_original_over_duckdb"], 2.0)
            self.assertEqual(q5_lan["runtime_delta_percent"], -50.0)
            markdown = reporter.render_markdown(data)
            self.assertIn("Original versus DuckDB-derived comparison", markdown)
            self.assertIn("DuckDB association", markdown)

    def test_main_writes_valid_json_and_markdown_for_complete_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            modes = (
                "plaintext-1pc/same",
                "secure-3pc/lan",
                "secure-3pc/wan-simulated",
            )
            profile = {
                "config": {"threads": 16, "scale_factor": 0.1},
                "raw_data": [
                    {"Overall": 1.0}, {"Overall": 2.0}, {"Overall": 3.0}
                ],
                "aggregated_data": {},
            }
            for mode in modes:
                run_dir = output_dir / mode
                run_dir.mkdir(parents=True)
                for query in reporter.QUERY_PLANS:
                    (run_dir / f"{query}.json").write_text(
                        json.dumps(profile), encoding="utf-8"
                    )
                    log_lines = ["[=SW] Start", "P0 Total 100"] * 3
                    if mode.endswith("wan-simulated"):
                        log_lines[0:0] = [
                            "Baseline ping latencies (before WAN simulation):",
                            "  node1: 0.200 ms",
                            "Ping latencies after enabling WAN simulation:",
                            "  node1: 13.200 ms",
                        ]
                    (run_dir / f"{query}.log").write_text(
                        "\n".join(log_lines), encoding="utf-8"
                    )

            result = reporter.main([
                "--output-dir", str(output_dir),
                "--scale-factor", "0.1",
                "--threads", "16",
                "--repetitions", "3",
                "--node-prefix", "node",
                "--wan-mode", "simulated",
            ])
            self.assertEqual(result, 0)
            structured = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(structured["benchmark"]["successful_executions"], 45)
            self.assertEqual(structured["failures"], [])
            self.assertTrue((output_dir / "plans.json").exists())
            self.assertIn("Run summary", (output_dir / "report.md").read_text(encoding="utf-8"))


class WanSimulationTests(unittest.TestCase):
    def test_interface_discovery_uses_system_host_resolver(self):
        with (
            mock.patch.object(runner.socket, "gethostbyname", return_value="10.0.0.2") as resolve,
            mock.patch.object(
                runner, "_run_cmd", side_effect=["10.0.0.2 via 10.0.0.1 dev eth0", "10.0.0.1/24"]
            ),
        ):
            interface, subnet = runner.discover_iface_and_subnet("node")
        resolve.assert_called_once_with("node1")
        self.assertEqual((interface, subnet), ("eth0", "10.0.0.1/24"))

    def test_wan_sim_choice_is_recorded_in_run_configuration(self):
        args = runner.build_parser().parse_args([
            "-s", "wan", "--wan-sim", "off", "-f", "0.1", "q1"
        ])
        with (
            mock.patch.object(runner, "discover_iface_and_subnet", return_value=(None, None)),
            mock.patch.object(runner, "_get_git_info", return_value=("abc123", "main", "")),
        ):
            cfg = runner.derive_config(args)
        run_config = runner._select_run_config(cfg, 0.1, "SF", 16, 0, ["./q1"])
        self.assertEqual(cfg.wan_sim, "off")
        self.assertEqual(run_config["wan_sim"], "off")

    def _run_main(self, wan_sim: str, run_side_effect=None):
        cfg = SimpleNamespace(exp_setting="wan", wan_sim=wan_sim)
        with (
            mock.patch.object(runner, "derive_config", return_value=cfg),
            mock.patch.object(runner, "perform_experiment_setup") as setup,
            mock.patch.object(runner, "run_experiments", side_effect=run_side_effect) as run,
            mock.patch.object(runner, "_manage_wan_simulation") as manage,
        ):
            if run_side_effect is None:
                result = runner.main(["--wan-sim", wan_sim, "-s", "wan", "q1"])
            else:
                with self.assertRaises(RuntimeError):
                    runner.main(["--wan-sim", wan_sim, "-s", "wan", "q1"])
                result = None
            return result, setup, run, manage

    def test_auto_enables_and_disables_simulation(self):
        result, setup, run, manage = self._run_main("auto")
        self.assertEqual(result, 0)
        setup.assert_called_once()
        run.assert_called_once()
        self.assertEqual(manage.call_args_list, [mock.call(mock.ANY, "on"), mock.call(mock.ANY, "off")])

    def test_off_never_changes_network(self):
        result, _, _, manage = self._run_main("off")
        self.assertEqual(result, 0)
        manage.assert_not_called()

    def test_auto_cleans_up_after_failure(self):
        _, _, _, manage = self._run_main("auto", RuntimeError("query failed"))
        self.assertEqual(manage.call_args_list, [mock.call(mock.ANY, "on"), mock.call(mock.ANY, "off")])

    def test_auto_cleans_up_when_enabling_raises(self):
        cfg = SimpleNamespace(exp_setting="wan", wan_sim="auto")
        with (
            mock.patch.object(runner, "derive_config", return_value=cfg),
            mock.patch.object(runner, "perform_experiment_setup") as setup,
            mock.patch.object(
                runner,
                "_manage_wan_simulation",
                side_effect=[RuntimeError("partial shaping"), None],
            ) as manage,
        ):
            with self.assertRaisesRegex(RuntimeError, "partial shaping"):
                runner.main(["--wan-sim", "auto", "-s", "wan", "q1"])

        setup.assert_not_called()
        self.assertEqual(
            manage.call_args_list,
            [mock.call(mock.ANY, "on"), mock.call(mock.ANY, "off")],
        )


class DryRunTests(unittest.TestCase):
    def test_dry_run_has_exact_matrix_and_no_1pc_network_modes(self):
        script = REPO_ROOT / "scripts" / "run-tpch-plain-and-3pc.sh"
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    "bash", str(script), "--dry-run", "--wan-mode", "real",
                    "--output-dir", str(Path(tmp) / "unused"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
        commands = [
            shlex.split(line.removeprefix("DRY RUN:"))
            for line in completed.stdout.splitlines() if line.startswith("DRY RUN:")
        ]
        self.assertEqual(len(commands), 15)

        matrix = []
        for command in commands:
            protocol = command[command.index("-p") + 1]
            setting = command[command.index("-s") + 1]
            query = command[-1]
            matrix.append((protocol, setting, query))

        self.assertEqual(sum(protocol == "1" and setting == "same" for protocol, setting, _ in matrix), 5)
        self.assertEqual(sum(protocol == "3" and setting == "lan" for protocol, setting, _ in matrix), 5)
        self.assertEqual(sum(protocol == "3" and setting == "wan" for protocol, setting, _ in matrix), 5)
        self.assertFalse(any(protocol == "1" and setting != "same" for protocol, setting, _ in matrix))

    def test_both_plan_set_has_30_groups_and_seeded_duckdb_targets(self):
        script = REPO_ROOT / "scripts" / "run-tpch-plain-and-3pc.sh"
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    "bash", str(script), "--dry-run", "--plan-set", "both",
                    "--data-seed", "42", "--output-dir", str(Path(tmp) / "unused"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
        commands = [
            shlex.split(line.removeprefix("DRY RUN:"))
            for line in completed.stdout.splitlines() if line.startswith("DRY RUN:")
        ]
        self.assertEqual(len(commands), 30)
        self.assertEqual(sum(command[-1].endswith("_duckdb") for command in commands), 15)
        self.assertTrue(all("-a=--tpch-seed=42" in command for command in commands))
        self.assertEqual(
            sum(command[command.index("-p") + 1] == "1" for command in commands), 10
        )
        self.assertFalse(any(
            command[command.index("-p") + 1] == "1"
            and command[command.index("-s") + 1] != "same"
            for command in commands
        ))


if __name__ == "__main__":
    unittest.main()
