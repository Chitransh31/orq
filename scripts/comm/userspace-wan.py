#!/usr/bin/env python3
"""No-sudo distributed NoCopy launcher with bounded, pipelined TCP relays.

6.5 ms per direction, 12 Gbit/s application-byte cap per directed party link.
This simulates application traffic, not packets, ACKs or kernel netem behavior.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import shlex
import signal
import socket
import subprocess
import sys
import time

DELAY = .0065
RATE = 12e9 / 8
CHUNK = 65536
QUEUE_CHUNKS = 256
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Relay:
    def __init__(self, hosts, delay=DELAY, rate=RATE):
        self.hosts = set(hosts)
        self.delay = delay
        self.rate = rate
        self.next_release = {}
        self.stats = {"connections": 0, "bytes": 0, "errors": [], "links": {}}
        self.tasks = set()
        self.stopping = False

    async def pipe(self, reader, writer, key):
        queue = asyncio.Queue(QUEUE_CHUNKS)
        loop = asyncio.get_running_loop()
        async def receive():
            try:
                while True:
                    data = await reader.read(CHUNK)
                    if not data:
                        await queue.put(None)
                        return
                    release = max(loop.time() + self.delay, self.next_release.get(key, 0)) + len(data) / self.rate
                    self.next_release[key] = release
                    await queue.put((release, data))
            except Exception as exc:
                await queue.put(exc)
        producer = asyncio.create_task(receive())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    if writer.can_write_eof():
                        writer.write_eof()
                        await writer.drain()
                    return
                release, data = item
                await asyncio.sleep(max(0, release - loop.time()))
                writer.write(data)
                await writer.drain()
                self.stats["bytes"] += len(data)
                self.stats["links"][key] = self.stats["links"].get(key, 0) + len(data)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote_writer = None
        pipes = []
        try:
            request = await asyncio.wait_for(reader.readline(), 10)
            host, port = request.decode().strip().split("\t")
            port = int(port)
            if host not in self.hosts or not 1024 <= port <= 65535:
                raise ValueError("Relay destination is not an allowed party endpoint")
            deadline = asyncio.get_running_loop().time() + 40
            while True:
                try:
                    remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
                    break
                except (OSError, asyncio.TimeoutError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError(f"Party endpoint {host}:{port} did not start")
                    await asyncio.sleep(.1)
            for stream in (writer, remote_writer):
                stream.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            writer.write(b"O")
            await writer.drain()
            self.stats["connections"] += 1
            pipes = [asyncio.create_task(self.pipe(reader, remote_writer, host + ":out")),
                     asyncio.create_task(self.pipe(remote_reader, writer, host + ":in"))]
            await asyncio.gather(*pipes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.stopping:
                self.stats["errors"].append(str(exc))
                print(f"Userspace relay failed: {exc}", file=sys.stderr, flush=True)
        finally:
            for pipe in pipes:
                pipe.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            for stream in (writer, remote_writer):
                if stream:
                    stream.close()
            self.tasks.discard(task)

    async def close(self):
        self.stopping = True
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def stop_process(child, sig=signal.SIGTERM):
    if child.returncode is not None:
        return
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(child.wait(), 5)
    except asyncio.TimeoutError:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await child.wait()


async def worker(payload):
    loop = asyncio.get_running_loop()
    cancelled = asyncio.Event()
    stop_signal = [signal.SIGTERM]
    def cancel(sig):
        if not cancelled.is_set():
            stop_signal[0] = sig
            cancelled.set()
    for sig in SIGNALS:
        loop.add_signal_handler(sig, cancel, sig)
    # Closing the SSH control input also tears down the owned party.
    def control_closed():
        if not os.read(sys.stdin.fileno(), 4096):
            loop.remove_reader(sys.stdin.fileno())
            cancel(signal.SIGTERM)
    loop.add_reader(sys.stdin.fileno(), control_closed)
    relay = Relay(payload["hosts"])
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = dict(os.environ, STARTMPC_EXEC_MODE="1", STARTMPC_HOST_COUNT="3",
               STARTMPC_HOST_RANK=str(payload["rank"]), STARTMPC_BASE_PORT=str(payload["base_port"]),
               STARTMPC_HOST_LIST=",".join(payload["hosts"]), ORQ_WAN_PROXY_PORT=str(port))
    child = None
    status = 1
    started = time.monotonic()
    try:
        if cancelled.is_set():
            return 128 + stop_signal[0]
        child = await asyncio.create_subprocess_exec("stdbuf", "-oL", "-eL", *payload["argv"], cwd=payload["cwd"], env=env,
                                                      start_new_session=True)
        done = asyncio.create_task(child.wait())
        cancel_wait = asyncio.create_task(cancelled.wait())
        await asyncio.wait([done, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
        if cancelled.is_set():
            await stop_process(child, stop_signal[0])
            status = 128 + stop_signal[0]
        else:
            status = child.returncode if child.returncode >= 0 else 128 - child.returncode
        cancel_wait.cancel()
        await asyncio.gather(cancel_wait, return_exceptions=True)
        # Allow completed streams to drain before recording final counters.
        if relay.tasks and status == 0:
            try:
                await asyncio.wait_for(asyncio.gather(*list(relay.tasks)), 2)
            except asyncio.TimeoutError:
                relay.stats["errors"].append("Relay streams did not finish after party exit")
        if relay.stats["errors"]:
            status = status or 1
    finally:
        if child:
            await stop_process(child)
        server.close()
        await server.wait_closed()
        await relay.close()
        print("[USERSPACE_WAN] " + json.dumps(dict(relay.stats, rank=payload["rank"],
              elapsed_seconds=time.monotonic() - started, delay_ms=DELAY * 1000,
              cap_gbps=RATE * 8 / 1e9, scope="party invocation", transport="TCP application-byte relay")), flush=True)
    return status


def launch(hosts, argv):
    if len(hosts) != 3 or len(set(hosts)) != 3:
        raise ValueError("Userspace distributed WAN requires three distinct hosts in party order")
    path = str(Path(__file__).resolve())
    # Copy the standalone worker to peers before any party is launched.
    for host in hosts[1:]:
        subprocess.run(["scp", "-q", path, f"{host}:{path}"], check=True)
    children = []
    cancelled = [0]
    def cancel(sig, frame):
        if not cancelled[0]:
            cancelled[0] = sig
    old = {sig: signal.signal(sig, cancel) for sig in SIGNALS}
    base_port = random.randrange(15000, 40000)
    status = 0
    try:
        for rank, host in enumerate(hosts):
            if cancelled[0]:
                break
            payload = dict(hosts=hosts, rank=rank, argv=argv, cwd=os.getcwd(), base_port=base_port)
            remote = shlex.join(["python3", path, "--worker", json.dumps(payload)])
            children.append(subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                               host, remote], stdin=subprocess.PIPE, start_new_session=True))
        while not cancelled[0]:
            codes = [child.poll() for child in children]
            failure = next((code for code in codes if code not in (None, 0)), None)
            if failure is not None:
                status = failure if failure >= 0 else 128 - failure
                break
            if all(code is not None for code in codes):
                break
            time.sleep(.05)
    finally:
        # EOF travels through SSH to each worker; it terminates only its owned party.
        for child in children:
            child.stdin.close()
        deadline = time.monotonic() + 8
        for child in children:
            try:
                child.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                status = status or 1
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return status or (128 + cancelled[0] if cancelled[0] else 0)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return asyncio.run(worker(json.loads(sys.argv[2])))
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-n", type=int, required=True)
    parser.add_argument("-h", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.n != 3 or not args.argv:
        parser.error("Expected -n 3 -h host0,host1,host2 COMMAND [ARGS]")
    return launch(args.h.split(","), args.argv)


if __name__ == "__main__":
    raise SystemExit(main())
