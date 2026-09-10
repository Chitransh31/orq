#!/usr/bin/env python3
"""Detached local supervisor for explicitly requested ORQ runs. No automatic restarts."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'results/tpch-run-registry'
TERMINAL = {'completed', 'failed', 'interrupted'}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def process_identity(pid):
    """PID plus boot/start identity prevents signaling a reused process number."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_ticks': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'host': socket.gethostname()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(identity):
    return bool(identity and process_identity(identity['pid']) == identity)


def safe_signal(identity, sig):
    if not alive(identity):
        return False
    # Linux pidfd pins the process across the identity check and signal delivery.
    try:with_fd = os.pidfd_open(identity['pid'])
    except ProcessLookupError:return False
    try:
        if not alive(identity):
            return False
        signal.pidfd_send_signal(with_fd, sig)
        return True
    finally:
        os.close(with_fd)


@contextmanager
def locked(path):
    with Path(path).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def observed_status(control):
    control = Path(control)
    status = read_json(control/'status.json', {'state': 'unknown'})
    spec = read_json(control/'spec.json', {})
    if spec.get('host') and spec['host'] != socket.gethostname():
        return dict(status, observed_state='unknown', reason='inspect on coordinator '+spec['host'])
    live_supervisor = alive(status.get('supervisor'))
    live_runner = alive(status.get('runner'))
    workload=read_json(Path(spec.get('output_dir',control))/'active_process.json',{})
    live_workload=alive(workload.get('process'))
    if status.get('state') in TERMINAL and not live_supervisor and not live_runner and not live_workload:
        observed = status['state']
    elif live_supervisor:
        age = time.time()-status.get('heartbeat_epoch', 0)
        observed = 'running' if age <= 30 else 'unknown'
    elif live_runner or live_workload:
        observed = 'unknown'  # Orphaned runner must not be duplicated.
    elif status.get('state') == 'starting' and time.time()-status.get('heartbeat_epoch',0) < 30:
        observed = 'starting'
    else:
        observed = 'stale'
    progress = read_json(Path(spec.get('output_dir', control))/'progress.json', {})
    return dict(status, observed_state=observed, supervisor_alive=live_supervisor,
                runner_alive=live_runner, workload_alive=live_workload, workload=workload, progress=progress, output_dir=spec.get('output_dir'))


def start_run(registry, run_id, spec=None, resume=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', run_id):
        raise ValueError('run ID must contain only letters, digits, dot, underscore, hyphen')
    registry = Path(registry).resolve()
    registry.mkdir(parents=True, exist_ok=True)
    control = registry/run_id
    with locked(registry/'.start.lock'):
        # Prevent two registered supervisors, even with different output directories.
        for other in registry.iterdir():
            if other.is_dir() and other != control and (other/'spec.json').exists():
                if observed_status(other)['observed_state'] in ('running','starting','unknown'):
                    raise ValueError('another registered run is active or uncertain: '+other.name)
        if resume:
            if not (control/'spec.json').exists():raise ValueError('unknown run ID')
            previous = observed_status(control)
            if previous['observed_state'] in ('running','starting','unknown','completed'):
                raise ValueError('refusing resume of '+previous['observed_state']+' run')
            spec = read_json(control/'spec.json')
            if spec['host'] != socket.gethostname():raise ValueError('resume on the recorded coordinator')
            if spec.get('kind') == 'experiment':
                from run_tpch_selectivity import source_hashes
                if spec['source_hashes'] != source_hashes():raise ValueError('source changed; cannot resume')
            command = [arg for arg in spec['command'] if arg != '--resume']
            if (Path(spec['output_dir'])/'manifest.json').exists():command.append('--resume')
            spec['command'] = command
            stamp = str(time.time_ns())
            for name in ('status.json', 'spec.json'):
                path = control/name
                if path.exists():path.rename(control/(stamp+'-'+name))
        else:
            if control.exists():raise ValueError('run ID already exists; inspect status or explicitly resume')
            if Path(spec['output_dir']).exists():raise ValueError('output directory already exists')
            control.mkdir()
        spec = dict(spec, run_id=run_id, host=socket.gethostname())
        write_json(control/'spec.json', spec)
        write_json(control/'status.json', {'state':'starting','started_at':now(),
                                          'heartbeat_epoch':time.time(),'run_id':run_id})
        with (control/'supervisor.log').open('ab', buffering=0) as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_watch', str(control)],
                                     cwd=spec['cwd'], stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        # Reap while this caller lives; daemon thread never holds the caller open.
        threading.Thread(target=child.wait,daemon=True).start()
        return {'run_id':run_id,'supervisor_pid':child.pid,'control_dir':str(control),
                'output_dir':spec['output_dir']}


def watch(control):
    control = Path(control)
    spec = read_json(control/'spec.json')
    state = {'run_id':spec['run_id'],'state':'running','started_at':now(),
             'supervisor':process_identity(os.getpid()),'runner':None}
    stopped = False
    child = None

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            safe_signal(state['runner'], signal.SIGTERM)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):signal.signal(sig, stop)

    def publish(**changes):
        state.update(changes, heartbeat_epoch=time.time(), heartbeat_at=now())
        write_json(control/'status.json', state)

    try:
        with locked(control.parent/'.supervisor.lock'):
            publish()
            if stopped:raise InterruptedError('stopped before launch')
            if spec.get('kind') == 'experiment':
                from run_tpch_selectivity import source_hashes
                if source_hashes() != spec['source_hashes']:raise ValueError('source changed before launch')
            with (control/'runner.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(spec['command'], cwd=spec['cwd'], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True, close_fds=True)
                state['runner'] = process_identity(child.pid)
                publish()
                if stopped:safe_signal(state['runner'], signal.SIGTERM)
                while child.poll() is None:
                    publish()
                    time.sleep(1)
                code = child.wait()
            complete = code == 0
            if spec.get('kind') == 'experiment':
                # Exit zero alone is insufficient; validate saved raw artifacts.
                import report_tpch_selectivity as reporter
                from export_tpch_presentation import export_report
                output = Path(spec['output_dir'])
                if (output/'manifest.json').exists():
                    data = reporter.build_report(output)
                    write_json(output/'results.json', data)
                    (output/'report.md').write_text(reporter.render_markdown(data))
                    export_report(data, output/'presentation')
                    complete = complete and data['complete']
                else:
                    complete = False
            progress = read_json(Path(spec['output_dir'])/'progress.json', {})
            interrupted = stopped or progress.get('state') == 'interrupted'
            publish(state='interrupted' if interrupted else ('completed' if complete else 'failed'),
                    exit_code=code, finished_at=now())
            return 0 if complete and not interrupted else 1
    except BaseException as error:
        if child is not None and child.poll() is None:
            safe_signal(state.get('runner'), signal.SIGTERM)
            # Keep supervising cleanup; never claim terminal while runner is alive.
            while child.poll() is None:
                publish(state='stopping', error=str(error))
                time.sleep(1)
        publish(state='interrupted' if stopped else 'failed', error=str(error),
                exit_code=child.returncode if child else None, finished_at=now())
        return 1


def experiment_spec(arguments):
    from run_tpch_selectivity import parser, source_hashes
    args = parser().parse_args(arguments)
    if not args.output_dir:raise ValueError('detached runs require --output-dir')
    if args.resume:raise ValueError('use supervisor resume instead of runner --resume')
    if args.dry_run:raise ValueError('use the runner directly for local dry runs')
    # Resolve paths now so later sessions do not depend on their working directory.
    output = args.output_dir.resolve()
    canonical = []
    skip = False
    for arg in arguments:
        if skip:skip=False;continue
        if arg in ('--output-dir','--correctness-dir'):skip=True;continue
        if arg.startswith(('--output-dir=','--correctness-dir=')):continue
        canonical.append(arg)
    canonical += ['--output-dir',str(output)]
    if args.correctness_dir:canonical += ['--correctness-dir',str(args.correctness_dir.resolve())]
    return {'kind':'experiment','command':[sys.executable,str(ROOT/'scripts/run_tpch_selectivity.py'),*canonical],
            'cwd':str(ROOT/'scripts'),'output_dir':str(output),'source_hashes':source_hashes(),
            'requested_at':now()}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry',type=Path,default=REGISTRY)
    sub = p.add_subparsers(dest='action',required=True)
    start = sub.add_parser('start');start.add_argument('run_id');start.add_argument('arguments',nargs=argparse.REMAINDER)
    status = sub.add_parser('status');status.add_argument('run_id',nargs='?')
    for action in ('resume','stop'):
        sub.add_parser(action).add_argument('run_id')
    sub.add_parser('_watch').add_argument('control',type=Path)
    args = p.parse_args(argv)
    if args.action=='_watch':return watch(args.control)
    if args.action=='start':
        arguments=args.arguments[1:] if args.arguments[:1]==['--'] else args.arguments
        result=start_run(args.registry,args.run_id,experiment_spec(arguments))
    elif args.action=='resume':result=start_run(args.registry,args.run_id,resume=True)
    elif args.action=='status':
        if args.run_id:
            control=args.registry/args.run_id
            if not (control/'spec.json').exists():raise ValueError('unknown run ID')
            result=observed_status(control)
        else:
            result={d.name:observed_status(d) for d in sorted(args.registry.glob('*'))
                    if d.is_dir() and (d/'spec.json').exists()}
    else:
        control=args.registry/args.run_id
        state=observed_status(control)
        if state.get('supervisor_alive'):
            delivered=safe_signal(state.get('supervisor'),signal.SIGTERM)
        elif state.get('runner_alive'):
            delivered=safe_signal(state.get('runner'),signal.SIGTERM)
        elif state.get('workload_alive'):
            delivered=safe_signal(state['workload'].get('process'),signal.SIGTERM)
        else:raise ValueError('no verified live process to stop')
        result={'signal_delivered':delivered,'status':observed_status(control)}
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as error:raise SystemExit(str(error))
