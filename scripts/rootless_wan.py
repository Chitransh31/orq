"""Rootless loopback WAN setup and process supervision (Linux only)."""
import ctypes
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid

CONTEXT = "ORQ_ROOTLESS_WAN_CONTEXT"
MAX_LIMIT = 2**32 - 1
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def positive(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be finite and positive") from None
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def profile(args):
    names = ("wan_latency_ms", "wan_bandwidth_gbps", "wan_loopback_mtu", "wan_queue_limit_packets")
    if args.wan_sim != "rootless-local":
        if args.wan_sim_check or any(getattr(args, name) is not None for name in names):
            raise ValueError("Rootless profile options and --wan-sim-check require --wan-sim rootless-local")
        return None
    comm = args.exp_communicator or ("mpi" if args.exp_setting == "same" else "nocopy")
    if (args.exp_protocol, args.exp_setting, comm) != (3, "same", "nocopy"):
        raise ValueError("rootless-local requires -p 3 -s same -c nocopy; example: "
                         "python3 run_experiment.py -p 3 -s same -c nocopy --wan-sim rootless-local micro_primitives")
    latency = positive(args.wan_latency_ms if args.wan_latency_ms is not None else 6.5, "latency")
    bandwidth = positive(args.wan_bandwidth_gbps if args.wan_bandwidth_gbps is not None else 12, "bandwidth")
    mtu = args.wan_loopback_mtu if args.wan_loopback_mtu is not None else 1500
    if not 1280 <= mtu <= 65536:
        raise ValueError("loopback MTU must be in [1280, 65536]")
    computed = 2 * (bandwidth * 1e9 / 8) * (latency / 1000) / mtu
    limit = args.wan_queue_limit_packets
    if limit is None:
        if not math.isfinite(computed) or computed > MAX_LIMIT:
            raise ValueError("computed queue limit exceeds tc's uint32 range; specify a supported profile")
        limit = max(1000, math.ceil(computed))
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"queue limit must be in [1, {MAX_LIMIT}]")
    return dict(latency_ms=latency, bandwidth_gbps=bandwidth, mtu=mtu, queue_limit_packets=limit)


def binary_options(tokens):
    """Match cryptoTools CLP: one leading dash, subsequent values until an option."""
    result = []
    for token in tokens:
        if (re.match(r"^-(?:l|w|latency|bandwidth|s|setting)=", token) or
                re.match(r"^--(?:l|w|latency|bandwidth|s|setting)(?:=|$)", token) or
                re.match(r"^-[lw][+\d.]", token)):
            raise ValueError(f"Malformed binary hint/setting {token!r}; use a single dash and separate value")
        if token.startswith("-") and not (len(token) > 1 and token[1].isdigit()):
            result.append((token[1:], []))
        elif result:
            result[-1][1].append(token)
        else:
            raise ValueError(f"Binary argument has no option: {token!r}")
    return result


def validate_effective(cmake, binary, physical, build_dir, setting="same"):
    i = 0
    while i < len(cmake):
        token = cmake[i]
        if token in ("--build", "--install", "-P") or token.startswith(("-B", "-S")):
            raise ValueError(f"Rootless CMake arguments cannot redirect configuration: {token}")
        if token == "-D":
            i += 1
            if i == len(cmake):
                raise ValueError("Missing CMake -D definition")
            token = "-D" + cmake[i]
        match = re.fullmatch(r"-D(PROTOCOL|COMM)(?::[^=]+)?=(.*)", token)
        if match and match[2] != {"PROTOCOL": "3", "COMM": "NOCOPY"}[match[1]]:
            raise ValueError(f"Conflicting rootless CMake override: {token}")
        i += 1
    options = binary_options(binary)
    # The runtime reads a single file, splitting on whitespace (not shell syntax).
    files = [(key, values) for key, values in options if key in ("f", "file-args")]
    if files:
        if len(files) != 1 or len(files[0][1]) != 1:
            raise ValueError("Provide exactly one binary argument file")
        path = Path(files[0][1][0])
        if not path.is_absolute():
            path = Path(build_dir) / path
        options += binary_options(path.read_text().split())
    for key, values in options:
        if key in ("s", "setting") and values != [setting]:
            raise ValueError(f"Binary setting override must be -s {setting} or -setting {setting}")
    hints = {}
    for short, long, field in (("l", "latency", "latency_ms"), ("w", "bandwidth", "bandwidth_gbps")):
        supplied = [values for key, values in options if key in (short, long)]
        if supplied:
            if len(supplied) != 1 or len(supplied[0]) != 1:
                raise ValueError(f"Provide exactly one finite positive -{short}/-{long} value")
            value = positive(supplied[0][0], long)
            if value != physical[field]:
                print(f"Warning: cost-model {long} {value:g} differs from physical profile {physical[field]:g}", file=sys.stderr)
        else:
            value = physical[field]
            binary.extend([f"-{short}", str(value)])
        hints[field] = value
    return hints


def namespaces():
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in ("user", "net")}


def verify_namespaces(outer):
    current = namespaces()
    if not isinstance(outer, dict) or set(outer) != {"user", "net"} or any(
            not isinstance(outer[k], str) or not outer[k].startswith(k + ":[") or outer[k] == current[k]
            for k in current):
        raise ValueError("Namespace verification failed; both user and network identities must differ. "
                         "Run through --wan-sim rootless-local; no network changes were attempted.")
    return current


def prerequisites():
    if sys.platform != "linux":
        raise ValueError("rootless-local requires Linux")
    for command in ("unshare", "ip", "tc"):
        if not shutil.which(command):
            raise ValueError(f"Missing required executable {command!r}; install util-linux and iproute2 locally")


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Command {shlex.join(argv)} failed: {exc}; check util-linux/iproute2 availability") from exc
    if result.returncode:
        raise RuntimeError(f"Command {shlex.join(argv)} exited {result.returncode}: {result.stderr.strip()}. "
                           "Check the requested profile and namespace policy; an administrator may need to provide/load sch_netem.")
    return result.stdout


class Loopback:
    def __init__(self, physical, outer, check=lambda: None):
        self.check = check
        self.physical = physical
        self.outer = outer
        self.owned = False
        self.actual = None
        self.baseline = None

    def counters(self):
        return json.loads(command(["tc", "-j", "-s", "qdisc", "show", "dev", "lo"]))

    def setup(self, baseline=False):
        identity = verify_namespaces(self.outer)
        print(f"Verified private user/network namespaces: {identity}", flush=True)
        existing = self.counters()
        if any(q.get("kind") != "noqueue" for q in existing):
            raise ValueError(f"Unexpected loopback qdisc; refusing to replace it: {existing}")
        p = self.physical
        self.check()
        command(["ip", "link", "set", "dev", "lo", "up", "mtu", str(p["mtu"])])
        # Read effective MTU, including the default, rather than assuming the set succeeded.
        link = json.loads(command(["ip", "-j", "link", "show", "dev", "lo"]))
        if len(link) != 1 or link[0].get("mtu") != p["mtu"]:
            raise ValueError(f"Loopback MTU readback failed: {link}")
        if baseline:
            self.check()
            self.baseline = tcp_probe()
        self.check()
        command(["tc", "qdisc", "add", "dev", "lo", "root", "handle", "1:", "netem",
                 "delay", f'{p["latency_ms"]:g}ms', "rate", f'{p["bandwidth_gbps"]:g}Gbit',
                 "limit", str(p["queue_limit_packets"])])
        self.owned = True
        config = command(["tc", "qdisc", "show", "dev", "lo"]).strip()
        self.validate_readback(config)
        self.actual = {"mtu": link[0]["mtu"], "qdisc": config}
        return self.actual

    def validate_readback(self, config):
        p = self.physical
        delay = re.search(r"\bdelay\s+([\d.]+)(us|ms|s)\b", config)
        rate = re.search(r"\brate\s+([\d.]+)([KMGT]?bit)\b", config, re.I)
        limit = re.search(r"\blimit\s+(\d+)(?:p)?\b", config)
        if not (re.search(r"\bnetem 1: root\b", config) and delay and rate and limit):
            raise ValueError(f"Incomplete netem readback: {config}")
        delay_ms = float(delay[1]) * {"us": .001, "ms": 1, "s": 1000}[delay[2]]
        rate_gbps = float(rate[1]) * {"bit": 1e-9, "kbit": 1e-6, "mbit": .001, "gbit": 1, "tbit": 1000}[rate[2].lower()]
        if not (math.isclose(delay_ms, p["latency_ms"], rel_tol=.005) and
                math.isclose(rate_gbps, p["bandwidth_gbps"], rel_tol=.005) and
                int(limit[1]) == p["queue_limit_packets"]):
            raise ValueError(f"Netem readback does not match requested profile {p}: {config}")

    def cleanup(self):
        if self.owned:
            command(["tc", "qdisc", "del", "dev", "lo", "root", "handle", "1:"])
            self.owned = False


def tcp_probe():
    """Small immediate TCP replies; sockets share one overall 30 second deadline."""
    deadline = time.monotonic() + 30
    errors = []
    samples = []
    def bounded(sock):
        sock.settimeout(max(.001, deadline - time.monotonic()))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with socket.socket() as server:
        bounded(server)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        def echo():
            try:
                conn, _ = server.accept()
                with conn:
                    bounded(conn)
                    for _ in range(7):
                        if conn.recv(1) != b"x":
                            raise RuntimeError("TCP probe ended early")
                        conn.sendall(b"x")
            except Exception as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        with socket.socket() as client:
            bounded(client)
            client.connect(server.getsockname())
            for _ in range(7):
                bounded(client)
                start = time.monotonic()
                client.sendall(b"x")
                if client.recv(1) != b"x":
                    raise RuntimeError("TCP probe reply missing")
                samples.append((time.monotonic() - start) * 1000)
        thread.join(max(0, deadline - time.monotonic()))
        if thread.is_alive() or errors:
            raise RuntimeError(f"TCP probe failed or timed out: {errors}")
    return {"samples_ms": samples, "median_ms": statistics.median(samples)}


def exit_status(code):
    return code if code >= 0 else 128 - code


class Supervisor:
    """Forward catchable signals; reap adopted descendants before network cleanup."""
    def __init__(self, subreaper=False, delegate=False):
        self.cancelled = 0
        self.child = None
        self.old = {}
        self.delegate = delegate
        self.subreaper = subreaper

    def __enter__(self):
        if self.subreaper:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
                raise OSError(ctypes.get_errno(), "Unable to become child subreaper")
        for sig in SIGNALS:
            self.old[sig] = signal.signal(sig, self.cancel)
        return self

    def cancel(self, sig, frame):
        if not self.cancelled:
            self.cancelled = sig

    def check(self):
        if self.cancelled:
            raise InterruptedError(f"Cancelled by signal {self.cancelled}")

    def adopted(self):
        if not self.subreaper:
            return []
        path = Path(f"/proc/self/task/{os.getpid()}/children")
        return [int(pid) for pid in path.read_text().split()]

    def signal_owned(self, sig):
        if self.child:
            try:
                if self.delegate:
                    os.kill(self.child.pid, sig)
                else:
                    os.killpg(self.child.pid, sig)
            except ProcessLookupError:
                pass
        for pid in self.adopted():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def remaining(self):
        # Popen must collect its own status before reaping adopted orphans.
        if self.child and self.child.poll() is None:
            return True
        if self.subreaper:
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if not pid:
                        break
                except ChildProcessError:
                    break
        group = False
        if self.child:
            try:
                os.killpg(self.child.pid, 0)
                group = True
            except ProcessLookupError:
                pass
        return group or bool(self.adopted())

    def run(self, argv, env=None):
        self.check()
        self.child = subprocess.Popen(argv, env=env, start_new_session=True)
        deadline = None
        while self.child.poll() is None:
            if self.cancelled:
                if deadline is None:
                    self.signal_owned(self.cancelled)
                    deadline = time.monotonic() + 5
                elif not self.delegate and time.monotonic() >= deadline:
                    self.signal_owned(signal.SIGKILL)
            time.sleep(.02)
        code = exit_status(self.child.returncode)
        if self.cancelled:
            code = 128 + self.cancelled
        # Also clean up children left behind by a successfully exiting runner.
        self.signal_owned(self.cancelled or signal.SIGTERM)
        if deadline is None:
            deadline = time.monotonic() + 5
        while self.remaining() and time.monotonic() < deadline:
            self.signal_owned(self.cancelled or signal.SIGTERM)
            time.sleep(.02)
        self.signal_owned(signal.SIGKILL)
        reap_deadline = time.monotonic() + 2
        while self.remaining() and time.monotonic() < reap_deadline:
            self.signal_owned(signal.SIGKILL)
            time.sleep(.02)
        if self.remaining():
            print("Failed to reap all owned workload descendants", file=sys.stderr)
            return code or 1
        return code or (128 + self.cancelled if self.cancelled else 0)

    def __exit__(self, *exc):
        if self.child and self.remaining():
            self.signal_owned(signal.SIGKILL)
            self.child.wait()
        for sig, handler in self.old.items():
            signal.signal(sig, handler)


def enter(argv, physical, check):
    prerequisites()
    runner = Path(__file__).with_name("run_experiment.py").resolve()
    helper = runner.parent / "comm" / "rootless-wan-sim.py"
    if not runner.is_file() or not helper.is_file() or not os.access(sys.executable, os.X_OK):
        raise ValueError("Missing runner/helper or Python interpreter; restore this repository checkout")
    if not check:
        launcher = runner.parents[1] / "include/backend/nocopy_communicator/startmpc/startmpc"
        if not os.access(launcher, os.X_OK):
            raise ValueError(f"Required repository launcher is missing or not executable: {launcher}")
        if not shutil.which("stdbuf"):
            raise ValueError("Missing required executable 'stdbuf'; install coreutils for local NoCopy execution")
    payload = {"outer": namespaces(), "physical": physical, "check": check,
               "argv": [sys.executable, str(runner), *argv]}
    cmd = ["unshare", "-Urn", "--", sys.executable, str(helper), json.dumps(payload)]
    with Supervisor(delegate=True) as supervisor:
        try:
            status = supervisor.run(cmd)
        except InterruptedError:
            status = 128 + supervisor.cancelled
    if status:
        print(f"Rootless command {shlex.join(cmd)} exited {status}. If namespace creation was denied, "
              "ask the site administrator about user-namespace limits/security restrictions; "
              "the tool stderr above identifies the failure.", file=sys.stderr)
    return status


def attach_diagnostics(path, invocation):
    if not path.exists():
        return
    decoder = json.JSONDecoder()
    source = path.read_text().strip()
    records = []
    matched = False
    while source:
        record, end = decoder.raw_decode(source)
        if record.get("rootless_invocation", {}).get("id") == invocation["id"]:
            matched = True
            record["rootless_invocation"] = invocation
        records.append(record)
        source = source[end:].lstrip()
    if not matched:
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text("".join(json.dumps(record, indent=2) + "\n" for record in records))
    tmp.replace(path)


def helper_main(payload):
    physical = payload["physical"]
    loopback = Loopback(physical, payload["outer"])
    diagnostics = {"id": str(uuid.uuid4()), "scope": "full invocation", "errors": []}
    status = 0
    started = time.monotonic()
    with Supervisor(subreaper=True) as supervisor:
        loopback.check = supervisor.check
        try:
            supervisor.check()
            diagnostics["namespaces"] = verify_namespaces(payload["outer"])
            actual = loopback.setup(baseline=payload["check"])
            diagnostics["actual"] = actual
            diagnostics["physical"] = physical
            if payload["check"]:
                diagnostics["baseline_probe"] = loopback.baseline
            supervisor.check()
            diagnostics["before"] = loopback.counters()
            if payload["check"]:
                diagnostics["shaped_probe"] = tcp_probe()
            else:
                context = {"outer": payload["outer"], "inner": namespaces(), "actual": actual,
                           "physical": physical, "invocation": diagnostics}
                env = dict(os.environ, **{CONTEXT: json.dumps(context)})
                status = supervisor.run(payload["argv"], env=env)
        except Exception as exc:
            print(f"rootless-local: {exc}", file=sys.stderr)
            diagnostics["errors"].append(str(exc))
            status = 128 + supervisor.cancelled if supervisor.cancelled else 1
        finally:
            if loopback.owned:
                try:
                    diagnostics["after"] = loopback.counters()
                except Exception as exc:
                    diagnostics["errors"].append(str(exc))
                    print(f"Final qdisc counters unavailable: {exc}", file=sys.stderr)
                    status = status or 1
            try:
                loopback.cleanup()
            except Exception as exc:
                diagnostics["errors"].append(str(exc))
                print(f"Qdisc cleanup failed: {exc}", file=sys.stderr)
                status = status or 1
            diagnostics["elapsed_seconds"] = time.monotonic() - started
            if not payload["check"]:
                try:
                    attach_diagnostics(Path.cwd().parent / "build" / "output.json", diagnostics)
                except Exception as exc:
                    print(f"Unable to record invocation diagnostics: {exc}", file=sys.stderr)
                    status = status or 1
            print("Rootless invocation diagnostics: " + json.dumps(diagnostics), flush=True)
    return status or (128 + supervisor.cancelled if supervisor.cancelled else 0)
