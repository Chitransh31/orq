"""Ordinary tests never mutate networking; integration requires explicit opt-in."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_experiment as runner
import rootless_wan as wan

LAUNCHER = ROOT / "include/backend/nocopy_communicator/startmpc/startmpc"
BASE = ["-p", "3", "-s", "same", "-c", "nocopy", "--wan-sim", "rootless-local"]
PHYSICAL = dict(latency_ms=6.5, bandwidth_gbps=12., mtu=1500, queue_limit_packets=13000)
OUTER = {"user": "user:[1]", "net": "net:[2]"}
INNER = {"user": "user:[3]", "net": "net:[4]"}
READBACK = "qdisc netem 1: root refcnt 2 limit 13000 delay 6.5ms rate 12Gbit"


def config(extra=(), base=BASE):
    with mock.patch.object(runner, "_get_git_info", return_value=(None, None, None)):
        return runner.derive_config(runner.build_parser().parse_args([*base, *extra, "micro_primitives"]))


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_hints(self):
        cfg = config()
        self.assertEqual(cfg.rootless["physical"], PHYSICAL)
        self.assertEqual(runner._flatten_args(cfg.exp_args), ["-l", "6.5", "-w", "12.0"])
        self.assertIn(str(LAUNCHER), cfg.run_cmd)
        self.assertEqual(config(base=[]).exp_communicator, "mpi")
        self.assertIsNone(config(base=[]).rootless)

    def test_invalid_profiles(self):
        for extra in (["--wan-latency-ms", "nan"], ["--wan-bandwidth-gbps", "inf"],
                      ["--wan-latency-ms", "0"], ["--wan-bandwidth-gbps", "-1"],
                      ["--wan-loopback-mtu", "1279"], ["--wan-loopback-mtu", "65537"],
                      ["--wan-queue-limit-packets", "0"], ["--wan-queue-limit-packets", str(2**32)],
                      ["--wan-bandwidth-gbps", "1e300"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                config(extra)
        self.assertEqual(config(["--wan-queue-limit-packets", "2000"]).rootless["physical"]["queue_limit_packets"], 2000)

    def test_modes_and_remote_discovery(self):
        with mock.patch.object(runner, "discover_iface_and_subnet") as discovery:
            for base in (["--wan-sim", "rootless-local"], BASE + ["-p", "2"], BASE + ["-s", "wan"]):
                with self.assertRaises(SystemExit):
                    config(base=base)
            discovery.assert_not_called()
        for mode in ("auto", "off"):
            for extra in (["--wan-sim-check"], ["--wan-latency-ms", "6.5"],
                          ["--wan-bandwidth-gbps", "12"], ["--wan-loopback-mtu", "1500"],
                          ["--wan-queue-limit-packets", "13000"]):
                with self.subTest(mode=mode, extra=extra), self.assertRaises(SystemExit):
                    config(extra, ["--wan-sim", mode])

    def test_effective_overrides(self):
        for value in ("-DPROTOCOL=2", "-D PROTOCOL:STRING=4", "-DCOMM:STRING=MPI", "-B elsewhere"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                config(["-m=" + value])
        config(["-m=-DPROTOCOL:STRING=3 -D COMM=NOCOPY"])
        for value in ("-s wan", "-setting lan", "-l 6.5 -latency 6.5", "-w 12 -w 12",
                      "-l", "-w NaN", "-latency 0", "-l 4 extra", "-l=4", "--latency 4", "-l4"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                config(["-a=" + value])
        cfg = config(['-a=-label "space ; $HOME" -l 6.5 -bandwidth 12 -setting same'])
        self.assertIn("space ; $HOME", runner._flatten_args(cfg.exp_args))
        self.assertEqual(cfg.rootless["cost_model"]["bandwidth_gbps"], 12)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            cfg = config(["-a=-latency 7 -w 13"])
        self.assertIn("differs from physical", errors.getvalue())
        self.assertEqual(cfg.rootless["cost_model"]["latency_ms"], 7)

    def test_argument_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "args"
            path.write_text("-setting wan")
            with self.assertRaises(SystemExit):
                config([f"-a=-f {path}"])
            path.write_text("-latency 6.5 -bandwidth 12")
            cfg = config([f"-a=-file-args {path}"])
            self.assertEqual(runner._flatten_args(cfg.exp_args), ["-file-args", str(path)])

    def test_cache_checked_before_make(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner.os, "getcwd", return_value=tmp + "/scripts"), \
                mock.patch.object(runner.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            Path(tmp, "build").mkdir()
            Path(tmp, "build", "CMakeCache.txt").write_text("PROTOCOL:STRING=3\nCOMM:STRING=MPI\n")
            with self.assertRaises(SystemExit):
                runner.perform_experiment_setup(cfg)
            self.assertEqual(run.call_count, 1)

    def test_managed_marker_and_metadata(self):
        context = dict(outer=OUTER, inner=INNER, physical=PHYSICAL, actual={"qdisc": READBACK},
                       invocation={"id": "test", "scope": "full invocation", "before": []})
        with mock.patch.dict(os.environ, {wan.CONTEXT: json.dumps(context)}), \
                mock.patch.object(wan, "namespaces", return_value=INNER):
            cfg = config()
            record = runner._select_run_config(cfg, 1, "rows", 1, 0, ["cmd"])
            self.assertEqual(record["wan_sim"], "rootless-local")
            self.assertTrue(record["rootless"]["managed_shaping"])
            self.assertNotIn("invocation", record["rootless"])
            with mock.patch.object(runner, "perform_experiment_setup"), mock.patch.object(runner, "run_experiments"), \
                    mock.patch.object(wan, "enter") as enter, mock.patch.object(runner, "_manage_wan_simulation") as cluster:
                self.assertEqual(runner.main([*BASE, "micro_primitives"]), 0)
                enter.assert_not_called()
                cluster.assert_not_called()
        with mock.patch.dict(os.environ, {wan.CONTEXT: json.dumps(context)}), \
                mock.patch.object(wan, "namespaces", return_value=OUTER), self.assertRaises(SystemExit):
            config()

    def test_entry_argument_boundaries_and_cwd(self):
        args = BASE + ["-a=-label 'two words ; $literal'", "micro_primitives"]
        cwd = os.getcwd()
        with mock.patch.object(wan, "prerequisites"), mock.patch.object(wan, "namespaces", return_value=OUTER), \
                mock.patch.object(wan.Supervisor, "run", return_value=0) as run:
            self.assertEqual(wan.enter(args, PHYSICAL, False), 0)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["unshare", "-Urn", "--"])
        self.assertEqual(json.loads(cmd[-1])["argv"][2:], args)
        self.assertEqual(os.getcwd(), cwd)


class NetworkUnitTests(unittest.TestCase):
    def setUp(self):
        self.ns = mock.patch.object(wan, "namespaces", return_value=INNER)
        self.ns.start()
        self.addCleanup(self.ns.stop)

    def test_missing_commands(self):
        with mock.patch.object(wan.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "unshare"):
            wan.prerequisites()

    def test_identity_before_any_mutation(self):
        for outer in (None, {}, INNER, dict(OUTER, net=INNER["net"])):
            with mock.patch.object(wan, "command") as command, self.assertRaises(ValueError):
                wan.Loopback(PHYSICAL, outer).setup()
            command.assert_not_called()

    def test_unexpected_qdisc(self):
        with mock.patch.object(wan, "command", return_value='[{"kind":"fq_codel"}]') as command:
            loop = wan.Loopback(PHYSICAL, OUTER)
            with self.assertRaises(ValueError):
                loop.setup()
            loop.cleanup()
            self.assertEqual(command.call_count, 1)

    def test_setup_readback_and_owned_cleanup(self):
        responses = ['[{"kind":"noqueue"}]', '', '[{"mtu":1500}]', '', READBACK, '']
        with mock.patch.object(wan, "command", side_effect=responses) as command:
            loop = wan.Loopback(PHYSICAL, OUTER)
            self.assertEqual(loop.setup()["mtu"], 1500)
            loop.cleanup()
            loop.cleanup()
            self.assertEqual(command.call_count, 6)
            self.assertIn("13000", command.call_args_list[3].args[0])
        for failed_at in (3, 4):
            responses = ['[]', '', '[{"mtu":1500}]', '', READBACK]
            responses[failed_at] = RuntimeError("setup failed")
            with mock.patch.object(wan, "command", side_effect=responses[:failed_at + 1] + ['']) as command:
                loop = wan.Loopback(PHYSICAL, OUTER)
                with self.assertRaises(RuntimeError):
                    loop.setup()
                loop.cleanup()
                self.assertEqual(command.call_count, failed_at + 1 + (failed_at == 4))

    def test_readback_mismatch(self):
        for config in ("", READBACK.replace("13000", "1000"), READBACK.replace("6.5ms", "1ms"),
                       READBACK.replace("12Gbit", "1Gbit")):
            with self.assertRaises(ValueError):
                wan.Loopback(PHYSICAL, OUTER).validate_readback(config)

    def test_command_error_has_diagnostics(self):
        with mock.patch.object(wan.subprocess, "run", return_value=mock.Mock(returncode=2, stderr="no netem")):
            with self.assertRaisesRegex(RuntimeError, "exited 2.*no netem.*sch_netem"):
                wan.command(["tc", "qdisc", "add"])

    def test_check_only_and_failure_precedence(self):
        payload = dict(physical=PHYSICAL, outer=OUTER, check=True, argv=["never"])
        with mock.patch.object(wan, "Loopback") as loop, mock.patch.object(wan, "Supervisor") as supervisor, \
                mock.patch.object(wan, "command"), mock.patch.object(wan, "tcp_probe", return_value={"median_ms": 13}), \
                mock.patch.object(wan, "attach_diagnostics") as attach:
            supervisor.return_value.__enter__.return_value.cancelled = 0
            loop.return_value.setup.return_value = {"mtu": 1500, "qdisc": READBACK}
            loop.return_value.baseline = {"median_ms": 0.1}
            loop.return_value.owned = True
            loop.return_value.counters.return_value = []
            self.assertEqual(wan.helper_main(payload), 0)
            supervisor.return_value.__enter__.return_value.run.assert_not_called()
            attach.assert_not_called()
            loop.return_value.cleanup.assert_called_once()
            payload["check"] = False
            loop.return_value.setup.return_value = {}
            supervisor.return_value.__enter__.return_value.run.return_value = 7
            loop.return_value.cleanup.side_effect = RuntimeError("cleanup")
            self.assertEqual(wan.helper_main(payload), 7)
            supervisor.return_value.__enter__.return_value.run.return_value = 0
            self.assertEqual(wan.helper_main(payload), 1)

    def test_final_counters_failure_does_not_mask_workload(self):
        payload = dict(physical=PHYSICAL, outer=OUTER, check=False, argv=["fixture"])
        with mock.patch.object(wan, "Loopback") as loop, mock.patch.object(wan, "Supervisor") as supervisor,                 mock.patch.object(wan, "attach_diagnostics"):
            instance = loop.return_value
            instance.owned = True
            instance.setup.return_value = {"mtu": 1500, "qdisc": READBACK}
            instance.counters.side_effect = [[], RuntimeError("counter read failed")]
            supervisor.return_value.__enter__.return_value.cancelled = 0
            supervisor.return_value.__enter__.return_value.run.return_value = 9
            self.assertEqual(wan.helper_main(payload), 9)
            instance.cleanup.assert_called_once()

    def test_cancelled_setup_does_not_add_qdisc(self):
        with mock.patch.object(wan, "command", return_value='[]') as command:
            loop = wan.Loopback(PHYSICAL, OUTER, mock.Mock(side_effect=InterruptedError()))
            with self.assertRaises(InterruptedError):
                loop.setup()
            self.assertEqual(command.call_count, 1)

    def test_diagnostics_not_grouping_keys_and_serialization(self):
        cfg = config()
        records = [dict(config=runner._select_run_config(cfg, 1, "rows", 1, i, []), stopwatch={"a": i}) for i in range(2)]
        grouped = runner._group_experiment_records(records)
        self.assertEqual(len(grouped), 1)
        grouped[0]["rootless_invocation"] = {"id": "owned"}
        with tempfile.TemporaryDirectory() as tmp:
            runner._write_experiment_json(tmp, grouped)
            invocation = {"id": "owned", "before": [], "after": [{"packets": 10}], "scope": "full invocation"}
            wan.attach_diagnostics(Path(tmp) / "output.json", invocation)
            result = json.loads((Path(tmp) / "output.json").read_text())
        self.assertEqual(result["rootless_invocation"], invocation)
        self.assertNotIn("after", result["config"]["rootless"])


class LocalProcessTests(unittest.TestCase):
    def launch(self, code, *args):
        return subprocess.Popen([str(LAUNCHER), "-n", "3", sys.executable, "-c", code, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_launcher_argv(self):
        args = ["two words", "'quotes'", "; touch /tmp/never-rootless", "$(false)"]
        child = self.launch("import json,os,sys; print(json.dumps([os.environ['STARTMPC_HOST_RANK'],sys.argv[1:]]))", *args)
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual({record[0] for record in records}, {"0", "1", "2"})
        self.assertTrue(all(record[1] == args for record in records))

    def test_failure_prompt_and_unrelated_survives(self):
        unrelated = self.launch("import time; print('ready',flush=True); time.sleep(60)")
        unrelated.stdout.readline()
        try:
            start = time.monotonic()
            child = self.launch("import os,time,sys; sys.exit(9) if os.environ['STARTMPC_HOST_RANK']=='1' else time.sleep(60)")
            _, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 9, err)
            self.assertLess(time.monotonic() - start, 3)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.communicate(timeout=10)

    def test_launcher_signals_and_escalation(self):
        for sig in wan.SIGNALS:
            with self.subTest(sig=sig):
                child = self.launch("import time; print('ready',flush=True); time.sleep(60)")
                self.assertEqual(child.stdout.readline().strip(), "ready")
                child.send_signal(sig)
                child.communicate(timeout=9)
                self.assertEqual(child.returncode, 128 + sig)
        child = self.launch("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)")
        for _ in range(3):
            child.stdout.readline()
        start = time.monotonic()
        child.terminate()
        child.communicate(timeout=9)
        self.assertEqual(child.returncode, 143)
        self.assertGreaterEqual(time.monotonic() - start, 4.8)
        self.assertLess(time.monotonic() - start, 8)

    def test_supervisor_status_and_startup_cancellation(self):
        for code in (0, 7):
            with wan.Supervisor() as supervisor:
                self.assertEqual(supervisor.run([sys.executable, "-c", f"raise SystemExit({code})"]), code)
        with wan.Supervisor() as supervisor:
            self.assertEqual(supervisor.run([sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"]), 143)
        with wan.Supervisor() as supervisor, mock.patch.object(wan.subprocess, "Popen") as popen:
            supervisor.cancel(signal.SIGHUP, None)
            with self.assertRaises(InterruptedError):
                supervisor.run(["never"])
            popen.assert_not_called()

    def test_supervisor_reaps_launcher_and_detached_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            party = ("import os,pathlib,signal,subprocess,sys,time; "
                     "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                     "pathlib.Path(sys.argv[1],os.environ['STARTMPC_HOST_RANK']).write_text(str(os.getpid())); "
                     "print('ready',flush=True); time.sleep(60)")
            command = [str(LAUNCHER), "-n", "3", sys.executable, "-c", party, tmp]
            code = (f"import sys; sys.path.insert(0,{str(ROOT / 'scripts')!r}); import rootless_wan as w\n"
                    f"with w.Supervisor(subreaper=True) as s:\n    status=s.run({command!r})\n"
                    "raise SystemExit(status)")
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(3):
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                pids = [int(path.read_text()) for path in Path(tmp).iterdir()]
                child.terminate()
                time.sleep(.05)
                child.send_signal(signal.SIGHUP)  # First cancellation remains authoritative.
                _, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 143, err)
                self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in pids), pids)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_tcp_probe_unshaped(self):
        result = wan.tcp_probe()
        self.assertEqual(len(result["samples_ms"]), 7)
        self.assertGreater(result["median_ms"], 0)


@unittest.skipUnless(os.environ.get("ORQ_ROOTLESS_INTEGRATION") == "1", "opt in with ORQ_ROOTLESS_INTEGRATION=1")
class LinuxIntegrationTests(unittest.TestCase):
    def test_actual_setup_and_tcp_latency(self):
        before = subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True)
        self.addCleanup(lambda: self.assertEqual(
            subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True), before))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/run_experiment.py"), *BASE,
                                 "--wan-sim-check", "micro_primitives"], cwd=ROOT / "scripts",
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        diagnostic = json.loads(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                                     if line.startswith("Rootless invocation diagnostics:")))
        self.assertEqual(diagnostic["actual"]["mtu"], 1500)
        wan.Loopback(PHYSICAL, OUTER).validate_readback(diagnostic["actual"]["qdisc"])
        for kind, identity in wan.namespaces().items():
            self.assertNotEqual(diagnostic["namespaces"][kind], identity)
        added = diagnostic["shaped_probe"]["median_ms"] - diagnostic["baseline_probe"]["median_ms"]
        self.assertTrue(8 <= added <= 40, f"Added RTT {added} ms; load {os.getloadavg()}; {diagnostic}")

    def helper_command(self, argv):
        payload = dict(physical=PHYSICAL, outer=wan.namespaces(), check=False, argv=argv)
        return ["unshare", "-Urn", "--", sys.executable,
                str(ROOT / "scripts/comm/rootless-wan-sim.py"), json.dumps(payload)]

    def diagnostics(self, output):
        return json.loads(next(line.split(": ", 1)[1] for line in output.splitlines()
                               if line.startswith("Rootless invocation diagnostics:")))

    def test_same_binary_shaped_and_unshaped_workload(self):
        binary = ROOT / "build/micro_primitives"
        self.assertTrue(binary.is_file(), "Build micro_primitives with PROTOCOL=3 COMM=NOCOPY COMM_THREADS=1 first")
        cache = (ROOT / "build/CMakeCache.txt").read_text()
        for key, value in (("PROTOCOL", "3"), ("COMM", "NOCOPY"), ("COMM_THREADS", "1")):
            self.assertRegex(cache, rf"(?m)^{key}:[^=]+={value}$")
        argv = [str(LAUNCHER), "-n", "3", str(binary), "-s", "same", "-t", "1", "-r", "1024",
                "-l", "6.5", "-w", "12"]
        host = subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True)
        runtimes = []
        # Temporary working directory isolates stopwatch files from real experiments.
        with tempfile.TemporaryDirectory() as tmp:
            try:
                for cmd in (argv, self.helper_command(argv)):
                    start = time.monotonic()
                    result = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=180)
                    runtimes.append(time.monotonic() - start)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                diagnostic = self.diagnostics(result.stdout)
                before = next(q for q in diagnostic["before"] if q["kind"] == "netem")
                after = next(q for q in diagnostic["after"] if q["kind"] == "netem")
                self.assertGreater(after["packets"], before["packets"], diagnostic)
                self.assertGreater(after["bytes"], before["bytes"], diagnostic)
                print(f"Unshaped/shaped seconds: {runtimes}; invocation counters: {diagnostic}")
            finally:
                self.assertEqual(subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True), host)

    def test_namespace_lifecycle(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        host = subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True)
        try:
            for sig in (None, *wan.SIGNALS):
                with self.subTest(signal=sig), tempfile.TemporaryDirectory() as tmp:
                    party = ("import os,pathlib,sys,time; "
                             "pathlib.Path(sys.argv[1],os.environ['STARTMPC_HOST_RANK']).write_text(str(os.getpid())); "
                             "time.sleep(0.3); "
                             + ("sys.exit(9) if os.environ['STARTMPC_HOST_RANK']=='1' else time.sleep(60)"
                                if sig is None else "time.sleep(60)"))
                    argv = [str(LAUNCHER), "-n", "3", sys.executable, "-c", party, tmp]
                    child = subprocess.Popen(self.helper_command(argv), cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        deadline = time.monotonic() + 20
                        while len(list(Path(tmp).iterdir())) < 3 and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(.02)
                        if sig is not None:
                            child.send_signal(sig)
                        out, err = child.communicate(timeout=15)
                        self.assertEqual(child.returncode, 9 if sig is None else 128 + sig, out + err)
                        pids = [int(path.read_text()) for path in Path(tmp).iterdir()]
                        self.assertEqual(len(pids), 3, out + err)
                        self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in pids), pids)
                        self.assertIn("after", self.diagnostics(out))
                        self.assertIsNone(unrelated.poll())
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()
        finally:
            unrelated.terminate()
            unrelated.wait()
            self.assertEqual(subprocess.check_output(["tc", "qdisc", "show", "dev", "lo"], text=True), host)


if __name__ == "__main__":
    unittest.main()
